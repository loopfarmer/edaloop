//! Parser for Specctra session (`.ses`) files.
//!
//! Units come from the `resolution` statement that governs the routes. Specctra's
//! Y axis points up and KiCad's points down, so Y is negated on the way in. That
//! matches the coordinates KiCad writes in its own DSN export.
use std::collections::BTreeMap;

#[derive(Debug, Clone, PartialEq)]
enum Tok {
    Atom(String),
    List(Vec<Tok>),
}

impl Tok {
    fn head(&self) -> Option<&str> {
        match self {
            Tok::List(v) => match v.first() {
                Some(Tok::Atom(a)) => Some(a),
                _ => None,
            },
            Tok::Atom(_) => None,
        }
    }
    fn items(&self) -> &[Tok] {
        match self {
            Tok::List(v) => v,
            Tok::Atom(_) => &[],
        }
    }
    fn find(&self, head: &str) -> Option<&Tok> {
        self.items().iter().find(|t| t.head() == Some(head))
    }
    fn atom(&self) -> Option<&str> {
        match self {
            Tok::Atom(a) => Some(a),
            Tok::List(_) => None,
        }
    }
}

const MAX_DEPTH: usize = 256;

fn parse_tokens(src: &[u8]) -> Result<Tok, String> {
    let mut quote = b'"';
    let mut i = 0;
    let n = src.len();
    let mut stack: Vec<Vec<Tok>> = vec![];
    let mut done: Option<Tok> = None;
    while i < n {
        let c = src[i];
        if c.is_ascii_whitespace() {
            i += 1;
        } else if c == b'(' {
            if stack.len() >= MAX_DEPTH {
                return Err("nesting too deep".into());
            }
            stack.push(vec![]);
            i += 1;
        } else if c == b')' {
            let list = stack.pop().ok_or("unexpected ')'")?;
            let t = Tok::List(list);
            match stack.last_mut() {
                Some(p) => p.push(t),
                None => {
                    if done.is_some() {
                        return Err("more than one top-level expression".into());
                    }
                    done = Some(t);
                }
            }
            i += 1;
        } else {
            // `(string_quote ")` carries a lone quote character as its argument.
            if stack.last().and_then(|l| l.first()) == Some(&Tok::Atom("string_quote".into())) && stack.last().is_some_and(|l| l.len() == 1) {
                quote = c;
                stack.last_mut().ok_or("no list")?.push(Tok::Atom((c as char).to_string()));
                i += 1;
                continue;
            }
            let tok = if c == quote {
                let s = i + 1;
                let mut j = s;
                while j < n && src[j] != quote {
                    j += 1;
                }
                if j >= n {
                    return Err("unterminated quoted token".into());
                }
                i = j + 1;
                String::from_utf8_lossy(&src[s..j]).to_string()
            } else {
                let s = i;
                while i < n && !src[i].is_ascii_whitespace() && src[i] != b'(' && src[i] != b')' {
                    i += 1;
                }
                String::from_utf8_lossy(&src[s..i]).to_string()
            };
            stack.last_mut().ok_or("text outside any list")?.push(Tok::Atom(tok));
        }
    }
    if !stack.is_empty() {
        return Err("unclosed '('".into());
    }
    done.ok_or_else(|| "empty file".to_string())
}

#[derive(Debug, Clone)]
pub struct Wire {
    pub net: String,
    pub layer: String,
    pub width_nm: i64,
    pub points_nm: Vec<(i64, i64)>,
}

#[derive(Debug, Clone)]
pub struct Via {
    pub net: String,
    pub padstack: String,
    pub at_nm: (i64, i64),
}

#[derive(Debug, Clone, Default)]
pub struct Padstack {
    pub size_nm: Option<i64>,
    pub layers: Vec<String>,
}

#[derive(Debug, Clone, Default)]
pub struct Session {
    pub resolution_unit: String,
    pub resolution_value: f64,
    pub nm_per_unit: f64,
    pub wires: Vec<Wire>,
    pub vias: Vec<Via>,
    pub padstacks: BTreeMap<String, Padstack>,
    pub nets: Vec<String>,
    /// Wire shapes this reader can't turn into tracks, such as `qarc`.
    pub unsupported_wires: Vec<(String, String)>,
    pub placements: usize,
}

fn unit_nm(unit: &str) -> Option<f64> {
    Some(match unit.to_ascii_lowercase().as_str() {
        "inch" => 25_400_000.0,
        "mil" => 25_400.0,
        "cm" => 10_000_000.0,
        "mm" => 1_000_000.0,
        "um" => 1_000.0,
        _ => return None,
    })
}

fn read_resolution(list: &Tok) -> Result<Option<(String, f64)>, String> {
    let Some(r) = list.find("resolution") else { return Ok(None) };
    let it = r.items();
    let unit = it.get(1).and_then(Tok::atom).ok_or("resolution has no unit")?;
    let val: f64 = it.get(2).and_then(Tok::atom).and_then(|v| v.parse().ok()).ok_or("resolution has no value")?;
    if val <= 0.0 || !val.is_finite() {
        return Err(format!("resolution value {val} isn't positive"));
    }
    if unit_nm(unit).is_none() {
        return Err(format!("resolution unit '{unit}' isn't known"));
    }
    Ok(Some((unit.to_string(), val)))
}

fn num(t: Option<&Tok>, what: &str) -> Result<f64, String> {
    let a = t.and_then(Tok::atom).ok_or_else(|| format!("{what}: number is missing"))?;
    let v: f64 = a.parse().map_err(|_| format!("{what}: '{a}' isn't a number"))?;
    if v.is_finite() { Ok(v) } else { Err(format!("{what}: '{a}' isn't finite")) }
}

pub fn parse(src: &[u8]) -> Result<Session, String> {
    let root = parse_tokens(src)?;
    if root.head() != Some("session") {
        return Err(format!("the root is '{}', not 'session'", root.head().unwrap_or("")));
    }
    let routes = root.find("routes").ok_or("the session has no routes section")?;
    let placement = root.find("placement");
    let res = match read_resolution(routes)? {
        Some(r) => r,
        None => match read_resolution(&root)? {
            Some(r) => r,
            None => match placement.map(read_resolution).transpose()?.flatten() {
                Some(r) => r,
                None => return Err("no resolution statement applies to the routes".into()),
            },
        },
    };
    let nm_per_unit = unit_nm(&res.0).ok_or("unknown unit")? / res.1;
    let scale = |v: f64| (v * nm_per_unit).round() as i64;
    let mut s = Session { resolution_unit: res.0.clone(), resolution_value: res.1, nm_per_unit, ..Session::default() };
    s.placements = placement
        .map(|p| p.items().iter().filter(|c| c.head() == Some("component")).map(|c| c.items().iter().filter(|x| x.head() == Some("place")).count()).sum())
        .unwrap_or(0);

    if let Some(lib) = routes.find("library_out") {
        for ps in lib.items().iter().filter(|t| t.head() == Some("padstack")) {
            let name = ps.items().get(1).and_then(Tok::atom).ok_or("padstack has no name")?.to_string();
            let mut p = Padstack::default();
            for sh in ps.items().iter().filter(|t| t.head() == Some("shape")) {
                if let Some(c) = sh.find("circle") {
                    if let Some(l) = c.items().get(1).and_then(Tok::atom) {
                        p.layers.push(l.to_string());
                    }
                    if let Some(d) = c.items().get(2).and_then(Tok::atom).and_then(|d| d.parse::<f64>().ok()) {
                        p.size_nm = Some(scale(d));
                    }
                }
            }
            s.padstacks.insert(name, p);
        }
    }
    let net_out = routes.find("network_out").ok_or("the routes have no network_out section")?;
    for net in net_out.items().iter().filter(|t| t.head() == Some("net")) {
        let name = net.items().get(1).and_then(Tok::atom).ok_or("net has no name")?.to_string();
        s.nets.push(name.clone());
        for item in net.items() {
            match item.head() {
                Some("wire") => read_wire(item, &name, &scale, &mut s)?,
                Some("via") => {
                    let it = item.items();
                    let padstack = it.get(1).and_then(Tok::atom).ok_or("via has no padstack")?.to_string();
                    let x = num(it.get(2), "via x")?;
                    let y = num(it.get(3), "via y")?;
                    s.vias.push(Via { net: name.clone(), padstack, at_nm: (scale(x), -scale(y)) });
                }
                _ => {}
            }
        }
    }
    Ok(s)
}

fn read_wire(item: &Tok, net: &str, scale: &dyn Fn(f64) -> i64, s: &mut Session) -> Result<(), String> {
    let path = item.find("path").or_else(|| item.find("polyline_path"));
    let Some(path) = path else {
        let shape = item.items().iter().skip(1).find_map(Tok::head).unwrap_or("nothing").to_string();
        s.unsupported_wires.push((net.to_string(), shape));
        return Ok(());
    };
    let it = path.items();
    let layer = it.get(1).and_then(Tok::atom).ok_or("wire path has no layer")?.to_string();
    let width = num(it.get(2), "wire width")?;
    let coords = &it[3.min(it.len())..];
    if coords.len() < 4 || coords.len() % 2 != 0 {
        return Err(format!("wire on net {net} has {} coordinates", coords.len()));
    }
    let mut pts = Vec::with_capacity(coords.len() / 2);
    for c in coords.chunks(2) {
        pts.push((scale(num(c.first(), "wire x")?), -scale(num(c.get(1), "wire y")?)));
    }
    s.wires.push(Wire { net: net.to_string(), layer, width_nm: scale(width), points_nm: pts });
    Ok(())
}

/// Parse KiCad's padstack naming, `Via[0-3]_600:300_um`, into size and drill in nm.
pub fn padstack_name_dims(name: &str) -> Option<(i64, i64, usize, usize)> {
    let rest = name.strip_prefix("Via[")?;
    let (span, rest) = rest.split_once("]_")?;
    let (a, b) = span.split_once('-')?;
    let dims = rest.strip_suffix("_um")?;
    let (size, drill) = dims.split_once(':')?;
    let nm = |v: &str| v.parse::<f64>().ok().map(|f| (f * 1000.0).round() as i64);
    Some((nm(size)?, nm(drill)?, a.parse().ok()?, b.parse().ok()?))
}

#[cfg(test)]
mod tests {
    use super::*;

    const S: &str = r#"(session b
      (base_design b)
      (placement (resolution mil 1) (component "X" (place R1 0 0 front 0)))
      (routes
        (resolution um 10)
        (parser (string_quote ") (host_cad "KiCad's Pcbnew"))
        (library_out (padstack "Via[0-1]_600:300_um" (shape (circle F.Cu 6000 0 0)) (shape (circle B.Cu 6000 0 0))))
        (network_out
          (net "A B"
            (via "Via[0-1]_600:300_um" 10000 -20000 (type protect))
            (wire (path F.Cu 2500 10000 -20000 20000 -20000 20000 -30000) (type route)))
          (net GND
            (wire (qarc F.Cu 1 0 0 1 1 2 2))))))"#;

    #[test]
    fn honours_routes_resolution_and_flips_y() {
        let s = parse(S.as_bytes()).unwrap();
        assert_eq!(s.nm_per_unit, 100.0);
        assert_eq!(s.vias[0].at_nm, (1_000_000, 2_000_000));
        assert_eq!(s.wires[0].width_nm, 250_000);
        assert_eq!(s.wires[0].points_nm[2], (2_000_000, 3_000_000));
        assert_eq!(s.wires[0].net, "A B");
        assert_eq!(s.padstacks["Via[0-1]_600:300_um"].size_nm, Some(600_000));
        assert_eq!(s.unsupported_wires, vec![("GND".to_string(), "qarc".to_string())]);
        assert_eq!(s.placements, 1);
    }

    #[test]
    fn other_units() {
        let t = |res: &str| {
            let src = format!("(session b (routes (resolution {res}) (network_out (net N (via \"v\" 1000 0)))))");
            parse(src.as_bytes()).unwrap().vias[0].at_nm.0
        };
        assert_eq!(t("um 1"), 1_000_000);
        assert_eq!(t("um 10"), 100_000);
        assert_eq!(t("mil 1"), 25_400_000);
        assert_eq!(t("mm 1"), 1_000_000_000);
        assert_eq!(t("inch 1000"), 25_400_000);
    }

    #[test]
    fn rejects_bad_files() {
        for bad in [
            "(pcb)", "(session a)", "(session a (routes (network_out)))",
            "(session a (routes (resolution parsec 1) (network_out)))",
            "(session a (routes (resolution um 0) (network_out)))",
            "(session a (routes (resolution um 1) (network_out (net N (wire (path F.Cu 1 0 0 1))))))",
            "(session a (routes (resolution um 1) (network_out (net N (via v x 0)))))",
            "(session", "",
        ] {
            assert!(parse(bad.as_bytes()).is_err(), "{bad}");
        }
    }

    #[test]
    fn padstack_names() {
        assert_eq!(padstack_name_dims("Via[0-3]_560:200_um"), Some((560_000, 200_000, 0, 3)));
        assert_eq!(padstack_name_dims("MyVia"), None);
    }
}
