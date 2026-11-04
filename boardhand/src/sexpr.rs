//! S-expression parser that keeps each node's byte span, stored in a flat arena.
//!
//! Tokens are parenthesised lists, bare atoms and double-quoted strings with
//! backslash escapes. A `#` as the first non-blank character of a line starts a
//! comment. Comments and whitespace are not nodes, but they sit in the gaps
//! between spans and so are preserved by every byte splice.
use std::borrow::Cow;

const NONE: u32 = u32::MAX;
const MAX_DEPTH: usize = 512;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    List,
    Atom,
    Str,
}

#[derive(Clone, Copy, Debug)]
pub struct Node {
    pub kind: Kind,
    pub start: u32,
    pub end: u32,
    first_child: u32,
    next: u32,
}

pub type Id = u32;

#[derive(Debug, Clone)]
pub struct ParseError {
    pub offset: usize,
    pub line: usize,
    pub message: String,
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} at line {} (byte {})", self.message, self.line, self.offset)
    }
}

pub struct Tree {
    pub src: Vec<u8>,
    pub nodes: Vec<Node>,
    pub root: Id,
}

fn err(src: &[u8], offset: usize, message: &str) -> ParseError {
    let line = src[..offset.min(src.len())].iter().filter(|b| **b == b'\n').count() + 1;
    ParseError { offset, line, message: message.to_string() }
}

pub fn parse(src: Vec<u8>) -> Result<Tree, ParseError> {
    if src.len() >= u32::MAX as usize {
        return Err(err(&[], 0, "file too large"));
    }
    let mut nodes: Vec<Node> = Vec::with_capacity(src.len() / 8 + 8);
    // Open lists: (node id, last child id).
    let mut stack: Vec<(u32, u32)> = Vec::with_capacity(32);
    let mut root = NONE;
    let mut i = 0usize;
    let mut only_blank_on_line = true;
    let n = src.len();

    while i < n {
        let c = src[i];
        match c {
            b'\n' => {
                only_blank_on_line = true;
                i += 1;
                continue;
            }
            b' ' | b'\t' | b'\r' | 0x0c => {
                i += 1;
                continue;
            }
            b'#' if only_blank_on_line => {
                while i < n && src[i] != b'\n' {
                    i += 1;
                }
                continue;
            }
            _ => {}
        }
        only_blank_on_line = false;
        if c == b')' {
            let Some((id, _)) = stack.pop() else {
                return Err(err(&src, i, "unexpected ')'"));
            };
            i += 1;
            nodes[id as usize].end = i as u32;
            continue;
        }
        let id = nodes.len() as u32;
        let start = i;
        let kind = match c {
            b'(' => {
                i += 1;
                Kind::List
            }
            b'"' => {
                i += 1;
                loop {
                    if i >= n {
                        return Err(err(&src, start, "unterminated string"));
                    }
                    match src[i] {
                        b'\\' => i += 2,
                        b'"' => {
                            i += 1;
                            break;
                        }
                        _ => i += 1,
                    }
                }
                if i > n {
                    return Err(err(&src, start, "unterminated string"));
                }
                Kind::Str
            }
            _ => {
                while i < n && !matches!(src[i], b' ' | b'\t' | b'\r' | b'\n' | 0x0c | b'(' | b')' | b'"') {
                    i += 1;
                }
                Kind::Atom
            }
        };
        nodes.push(Node { kind, start: start as u32, end: i as u32, first_child: NONE, next: NONE });
        match stack.last_mut() {
            Some((parent, last)) => {
                if *last == NONE {
                    nodes[*parent as usize].first_child = id;
                } else {
                    nodes[*last as usize].next = id;
                }
                *last = id;
            }
            None => {
                if root != NONE {
                    return Err(err(&src, start, "more than one top-level expression"));
                }
                root = id;
            }
        }
        if kind == Kind::List {
            if stack.len() >= MAX_DEPTH {
                return Err(err(&src, start, "nesting too deep"));
            }
            stack.push((id, NONE));
        }
    }
    if let Some((id, _)) = stack.last() {
        let at = nodes[*id as usize].start as usize;
        return Err(err(&src, at, "unclosed '('"));
    }
    if root == NONE {
        return Err(err(&src, 0, "no expression found"));
    }
    Ok(Tree { src, nodes, root })
}

pub struct Children<'a> {
    tree: &'a Tree,
    cur: u32,
}

impl Iterator for Children<'_> {
    type Item = Id;
    fn next(&mut self) -> Option<Id> {
        if self.cur == NONE {
            return None;
        }
        let id = self.cur;
        self.cur = self.tree.nodes[id as usize].next;
        Some(id)
    }
}

impl Tree {
    pub fn node(&self, id: Id) -> Node {
        self.nodes[id as usize]
    }

    pub fn span(&self, id: Id) -> (usize, usize) {
        let n = self.node(id);
        (n.start as usize, n.end as usize)
    }

    pub fn bytes(&self, id: Id) -> &[u8] {
        let (s, e) = self.span(id);
        &self.src[s..e]
    }

    pub fn kind(&self, id: Id) -> Kind {
        self.node(id).kind
    }

    pub fn children(&self, id: Id) -> Children<'_> {
        Children { tree: self, cur: self.node(id).first_child }
    }

    /// The decoded value of an atom or a string; empty for a list.
    pub fn text(&self, id: Id) -> Cow<'_, str> {
        let b = self.bytes(id);
        match self.kind(id) {
            Kind::Atom => String::from_utf8_lossy(b),
            Kind::Str => Cow::Owned(unescape(&b[1..b.len() - 1])),
            Kind::List => Cow::Borrowed(""),
        }
    }

    /// The head symbol of a list: its first child, when that is a bare atom.
    pub fn head(&self, id: Id) -> &str {
        let first = self.node(id).first_child;
        if self.kind(id) != Kind::List || first == NONE || self.kind(first) != Kind::Atom {
            return "";
        }
        std::str::from_utf8(self.bytes(first)).unwrap_or("")
    }

    /// Direct child list with this head.
    pub fn find(&self, id: Id, head: &str) -> Option<Id> {
        self.children(id).find(|c| self.kind(*c) == Kind::List && self.head(*c) == head)
    }

    pub fn find_all<'a>(&'a self, id: Id, head: &'a str) -> impl Iterator<Item = Id> + 'a {
        self.children(id).filter(move |c| self.kind(*c) == Kind::List && self.head(*c) == head)
    }

    /// Children after the head.
    pub fn args(&self, id: Id) -> impl Iterator<Item = Id> + '_ {
        self.children(id).skip(1)
    }

    pub fn arg(&self, id: Id, n: usize) -> Option<Id> {
        self.args(id).nth(n)
    }

    pub fn arg_text(&self, id: Id, n: usize) -> Option<Cow<'_, str>> {
        self.arg(id, n).filter(|a| self.kind(*a) != Kind::List).map(|a| self.text(a))
    }

    pub fn arg_num(&self, id: Id, n: usize) -> Option<f64> {
        self.arg_text(id, n).and_then(|t| t.parse::<f64>().ok())
    }

    /// Reproduce the file by walking the tree: gap bytes and token bytes in turn.
    pub fn write_out(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(self.src.len());
        let mut cursor = 0usize;
        self.emit(self.root, &mut out, &mut cursor);
        out.extend_from_slice(&self.src[cursor..]);
        out
    }

    fn emit(&self, id: Id, out: &mut Vec<u8>, cursor: &mut usize) {
        let (s, e) = self.span(id);
        out.extend_from_slice(&self.src[*cursor..s]);
        *cursor = s;
        if self.kind(id) == Kind::List {
            out.push(b'(');
            *cursor = s + 1;
            for c in self.children(id) {
                self.emit(c, out, cursor);
            }
            out.extend_from_slice(&self.src[*cursor..e - 1]);
            out.push(b')');
        } else {
            out.extend_from_slice(&self.src[s..e]);
        }
        *cursor = e;
    }
}

fn unescape(b: &[u8]) -> String {
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] != b'\\' || i + 1 >= b.len() {
            out.push(b[i]);
            i += 1;
            continue;
        }
        let c = b[i + 1];
        i += 2;
        match c {
            b'n' => out.push(b'\n'),
            b'r' => out.push(b'\r'),
            b't' => out.push(b'\t'),
            b'a' => out.push(7),
            b'b' => out.push(8),
            b'f' => out.push(12),
            b'v' => out.push(11),
            b'x' => {
                let mut v = 0u32;
                let mut k = 0;
                while k < 2 && i < b.len() && (b[i] as char).is_ascii_hexdigit() {
                    v = v * 16 + (b[i] as char).to_digit(16).unwrap_or(0);
                    i += 1;
                    k += 1;
                }
                out.push(v as u8);
            }
            b'0'..=b'7' => {
                let mut v = u32::from(c - b'0');
                let mut k = 0;
                while k < 2 && i < b.len() && (b'0'..=b'7').contains(&b[i]) {
                    v = v * 8 + u32::from(b[i] - b'0');
                    i += 1;
                    k += 1;
                }
                out.push(v as u8);
            }
            other => out.push(other),
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// One replacement in the source file.
#[derive(Debug, Clone)]
pub struct Splice {
    pub start: usize,
    pub end: usize,
    pub text: Vec<u8>,
}

/// Apply splices, which must not overlap. Equal starts keep their given order.
pub fn apply_splices(src: &[u8], splices: &[Splice]) -> Vec<u8> {
    let mut sorted: Vec<&Splice> = splices.iter().collect();
    sorted.sort_by_key(|s| s.start);
    let mut out = Vec::with_capacity(src.len() + 256);
    let mut cursor = 0;
    for s in sorted {
        assert!(s.start >= cursor && s.end >= s.start && s.end <= src.len(), "overlapping splice");
        out.extend_from_slice(&src[cursor..s.start]);
        out.extend_from_slice(&s.text);
        cursor = s.end;
    }
    out.extend_from_slice(&src[cursor..]);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn t(s: &str) -> Tree {
        parse(s.as_bytes().to_vec()).unwrap()
    }

    #[test]
    fn roundtrip_with_comments_and_odd_formatting() {
        let s = "# leading comment (with parens\n( a  \"b\\\"c\" (d 1.50)\n  # inner )\n  x #PWR )\n\n";
        let parsed = t(s);
        assert_eq!(parsed.write_out(), s.as_bytes());
    }

    #[test]
    fn atom_and_string_are_distinct_and_decoded() {
        let p = t("(net 3)");
        assert_eq!(p.kind(p.arg(p.root, 0).unwrap()), Kind::Atom);
        let p = t(r#"(net "3\"\\x")"#);
        let a = p.arg(p.root, 0).unwrap();
        assert_eq!(p.kind(a), Kind::Str);
        assert_eq!(p.text(a), "3\"\\x");
    }

    #[test]
    fn errors() {
        for bad in ["(a", ")", "(a \"b)", "", "(a) (b)", "# only comment"] {
            assert!(parse(bad.as_bytes().to_vec()).is_err(), "{bad}");
        }
        let deep = "(".repeat(100_000);
        assert!(parse(deep.into_bytes()).is_err());
    }

    #[test]
    fn trailing_backslash_in_string_is_an_error_not_a_panic() {
        assert!(parse(b"(a \"b\\".to_vec()).is_err());
    }

    #[test]
    fn non_utf8_bytes_survive() {
        let s = b"(a \"\xff\xfe\" b)\n".to_vec();
        assert_eq!(parse(s.clone()).unwrap().write_out(), s);
    }

    #[test]
    fn spans_and_splices() {
        let p = t("(a (b 1) (c 2))");
        let c = p.find(p.root, "c").unwrap();
        let (s, e) = p.span(c);
        assert_eq!(&p.src[s..e], b"(c 2)");
        let out = apply_splices(&p.src, &[Splice { start: s, end: e, text: b"(c 3)".to_vec() }]);
        assert_eq!(out, b"(a (b 1) (c 3))");
    }
}
