//! Read-only model over a parsed `.kicad_pcb`. Every value comes from the file.
use crate::sexpr::{self, Id, Kind, ParseError, Tree};
use crate::util::{sha256_hex, sha256_raw, to_nm};
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashSet};

#[derive(Clone, Debug)]
pub struct Layer {
    pub name: String,
    pub kind: String,
    pub user_name: Option<String>,
}

impl Layer {
    pub fn is_copper(&self) -> bool {
        self.name.ends_with(".Cu")
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum NetForm {
    /// `(net 3)` referring to a table of `(net 3 "NAME")`.
    Numbered,
    /// `(net "NAME")`.
    Named,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum TrackKind {
    Segment,
    Arc,
    Via,
}

impl TrackKind {
    pub fn as_str(self) -> &'static str {
        match self {
            TrackKind::Segment => "segment",
            TrackKind::Arc => "arc",
            TrackKind::Via => "via",
        }
    }
}

#[derive(Clone, Debug)]
pub struct Track {
    pub node: Id,
    pub kind: TrackKind,
    pub uuid: Option<String>,
    pub net: Option<String>,
    pub net_number: Option<i64>,
    /// A single layer for segments and arcs; the listed pair for vias.
    pub layers: Vec<String>,
    pub start: (f64, f64),
    pub mid: Option<(f64, f64)>,
    pub end: (f64, f64),
    pub width: f64,
    pub drill: f64,
    pub via_type: String,
    pub locked: bool,
}

#[derive(Clone, Debug)]
pub struct Pad {
    pub number: String,
    pub net: Option<String>,
}

#[derive(Clone, Debug)]
pub struct Footprint {
    pub node: Id,
    pub id: String,
    pub uuid: Option<String>,
    pub reference: Option<String>,
    pub at: (f64, f64),
    pub rotation: f64,
    pub layer: String,
    pub locked: bool,
    pub pads: Vec<Pad>,
}

pub struct Board {
    pub tree: Tree,
    /// Direct children of the root after its head symbol.
    pub top: Vec<Id>,
    pub layers: Vec<Layer>,
    pub net_table: BTreeMap<i64, String>,
}

#[derive(Debug)]
pub enum BoardError {
    Parse(ParseError),
    NotABoard(String),
}

impl std::fmt::Display for BoardError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            BoardError::Parse(e) => write!(f, "{e}"),
            BoardError::NotABoard(h) => write!(f, "the file's root is '{h}', not 'kicad_pcb'"),
        }
    }
}

/// True for a direct `(locked yes)`, a bare `(locked)`, or the older inline atom `locked`.
pub fn node_locked(t: &Tree, id: Id) -> bool {
    for c in t.args(id) {
        match t.kind(c) {
            Kind::Atom if t.bytes(c) == b"locked" => return true,
            Kind::List if t.head(c) == "locked" => {
                return match t.arg_text(c, 0) {
                    None => true,
                    Some(v) => v == "yes" || v == "true",
                };
            }
            _ => {}
        }
    }
    false
}

/// The `uuid` of a node, or `tstamp` in older files.
pub fn node_uuid(t: &Tree, id: Id) -> Option<String> {
    t.find(id, "uuid")
        .or_else(|| t.find(id, "tstamp"))
        .and_then(|u| t.arg_text(u, 0))
        .map(|s| s.to_string())
}

fn xy(t: &Tree, id: Id, head: &str) -> Option<(f64, f64)> {
    let n = t.find(id, head)?;
    Some((t.arg_num(n, 0)?, t.arg_num(n, 1)?))
}

/// Rotate a footprint-local point by KiCad's convention (Y down, positive is
/// counter-clockwise on screen), then translate it.
pub fn local_to_board(p: (f64, f64), at: (f64, f64), rot_deg: f64) -> (f64, f64) {
    let r = rot_deg.to_radians();
    let (s, c) = r.sin_cos();
    (at.0 + p.0 * c + p.1 * s, at.1 - p.0 * s + p.1 * c)
}

impl Board {
    pub fn parse(bytes: Vec<u8>) -> Result<Board, BoardError> {
        let tree = sexpr::parse(bytes).map_err(BoardError::Parse)?;
        let head = tree.head(tree.root).to_string();
        if head != "kicad_pcb" {
            return Err(BoardError::NotABoard(head));
        }
        let top: Vec<Id> = tree.args(tree.root).collect();
        let mut layers = Vec::new();
        if let Some(ls) = tree.find(tree.root, "layers") {
            for l in tree.children(ls) {
                if tree.kind(l) != Kind::List {
                    continue;
                }
                let (Some(name), kind) = (tree.arg_text(l, 0), tree.arg_text(l, 1)) else { continue };
                layers.push(Layer {
                    name: name.to_string(),
                    kind: kind.map(|k| k.to_string()).unwrap_or_default(),
                    user_name: tree.arg_text(l, 2).map(|u| u.to_string()),
                });
            }
        }
        let mut net_table = BTreeMap::new();
        for &c in &top {
            if tree.kind(c) == Kind::List && tree.head(c) == "net" {
                if let (Some(num), Some(name)) = (tree.arg_text(c, 0), tree.arg_text(c, 1)) {
                    if let Ok(n) = num.parse::<i64>() {
                        net_table.insert(n, name.to_string());
                    }
                }
            }
        }
        Ok(Board { tree, top, layers, net_table })
    }

    pub fn revision(&self) -> String {
        sha256_hex(&self.tree.src)
    }

    pub fn digests(&self) -> Vec<[u8; 32]> {
        self.top.iter().map(|id| sha256_raw(self.tree.bytes(*id))).collect()
    }

    pub fn copper_layers(&self) -> Vec<&Layer> {
        self.layers.iter().filter(|l| l.is_copper()).collect()
    }

    /// Canonical copper layer name for a canonical name or a user alias.
    pub fn resolve_copper(&self, name: &str) -> Option<String> {
        let found = self
            .layers
            .iter()
            .find(|l| l.name == name)
            .or_else(|| self.layers.iter().find(|l| l.user_name.as_deref() == Some(name)))?;
        found.is_copper().then(|| found.name.clone())
    }

    /// Resolve a node's net reference to a name, handling both forms.
    pub fn node_net(&self, id: Id) -> (Option<String>, Option<i64>) {
        let t = &self.tree;
        let Some(n) = t.find(id, "net") else { return (None, None) };
        let Some(a) = t.arg(n, 0) else { return (None, None) };
        if t.kind(a) == Kind::Str {
            let s = t.text(a).to_string();
            return (if s.is_empty() { None } else { Some(s) }, None);
        }
        let text = t.text(a);
        match text.parse::<i64>() {
            Ok(num) => {
                let name = t
                    .arg_text(n, 1)
                    .map(|s| s.to_string())
                    .or_else(|| self.net_table.get(&num).cloned())
                    .filter(|s| !s.is_empty());
                (name, Some(num))
            }
            Err(_) => (Some(text.to_string()), None),
        }
    }

    /// The form this file uses for its net references.
    pub fn net_form(&self) -> NetForm {
        let t = &self.tree;
        for &c in &self.top {
            if matches!(t.head(c), "segment" | "arc" | "via") {
                if let Some(a) = t.find(c, "net").and_then(|n| t.arg(n, 0)) {
                    if t.kind(a) == Kind::Str {
                        return NetForm::Named;
                    }
                    if t.text(a).parse::<i64>().is_ok() {
                        return NetForm::Numbered;
                    }
                }
            }
        }
        if self.net_table.is_empty() {
            NetForm::Named
        } else {
            NetForm::Numbered
        }
    }

    pub fn net_number(&self, name: &str) -> Option<i64> {
        self.net_table.iter().find(|(_, v)| v.as_str() == name).map(|(k, _)| *k)
    }

    fn parse_track(&self, id: Id) -> Option<Track> {
        let t = &self.tree;
        let kind = match t.head(id) {
            "segment" => TrackKind::Segment,
            "arc" => TrackKind::Arc,
            "via" => TrackKind::Via,
            _ => return None,
        };
        let (net, net_number) = self.node_net(id);
        let (start, mid, end, width, drill, layers, via_type);
        if kind == TrackKind::Via {
            let at = xy(t, id, "at").unwrap_or((0.0, 0.0));
            start = at;
            mid = None;
            end = at;
            width = t.find(id, "size").and_then(|n| t.arg_num(n, 0)).unwrap_or(0.0);
            drill = t.find(id, "drill").and_then(|n| t.arg_num(n, 0)).unwrap_or(0.0);
            layers = t
                .find(id, "layers")
                .map(|l| t.args(l).map(|a| t.text(a).to_string()).collect())
                .unwrap_or_default();
            let mut vt = "through".to_string();
            for a in t.args(id) {
                if t.kind(a) == Kind::Atom && matches!(t.bytes(a), b"blind" | b"micro" | b"buried") {
                    vt = t.text(a).to_string();
                }
            }
            if let Some(ty) = t.find(id, "type").and_then(|n| t.arg_text(n, 0)) {
                vt = ty.to_string();
            }
            via_type = vt;
        } else {
            start = xy(t, id, "start").unwrap_or((0.0, 0.0));
            mid = xy(t, id, "mid");
            end = xy(t, id, "end").unwrap_or((0.0, 0.0));
            width = t.find(id, "width").and_then(|n| t.arg_num(n, 0)).unwrap_or(0.0);
            drill = 0.0;
            layers = t
                .find(id, "layer")
                .and_then(|n| t.arg_text(n, 0))
                .map(|s| vec![s.to_string()])
                .unwrap_or_default();
            via_type = String::new();
        }
        Some(Track {
            node: id,
            kind,
            uuid: node_uuid(t, id),
            net,
            net_number,
            layers,
            start,
            mid,
            end,
            width,
            drill,
            via_type,
            locked: node_locked(t, id),
        })
    }

    pub fn tracks(&self) -> Vec<Track> {
        self.top.iter().filter_map(|id| self.parse_track(*id)).collect()
    }

    /// Does this item occupy the copper layer? A via spans its listed pair.
    pub fn track_on_layer(&self, tr: &Track, layer: &str) -> bool {
        if tr.kind != TrackKind::Via {
            return tr.layers.iter().any(|l| l == layer);
        }
        let order: Vec<&str> = self.copper_layers().iter().map(|l| l.name.as_str()).collect();
        let pos = |n: &str| order.iter().position(|o| *o == n);
        let (Some(a), Some(b), Some(x)) = (
            tr.layers.first().and_then(|n| pos(n)),
            tr.layers.get(1).and_then(|n| pos(n)),
            pos(layer),
        ) else {
            return tr.layers.iter().any(|l| l == layer);
        };
        a.min(b) <= x && x <= a.max(b)
    }

    pub fn footprints(&self) -> Vec<Footprint> {
        let t = &self.tree;
        let mut out = Vec::new();
        for &id in &self.top {
            if t.kind(id) != Kind::List || t.head(id) != "footprint" {
                continue;
            }
            let mut reference = None;
            for p in t.find_all(id, "property") {
                if t.arg_text(p, 0).as_deref() == Some("Reference") {
                    reference = t.arg_text(p, 1).map(|s| s.to_string());
                }
            }
            if reference.is_none() {
                for p in t.find_all(id, "fp_text") {
                    if t.arg_text(p, 0).as_deref() == Some("reference") {
                        reference = t.arg_text(p, 1).map(|s| s.to_string());
                    }
                }
            }
            let at_node = t.find(id, "at");
            let pads = t
                .find_all(id, "pad")
                .map(|p| Pad {
                    number: t.arg_text(p, 0).map(|s| s.to_string()).unwrap_or_default(),
                    net: self.node_net(p).0,
                })
                .collect();
            out.push(Footprint {
                node: id,
                id: t.arg_text(id, 0).map(|s| s.to_string()).unwrap_or_default(),
                uuid: node_uuid(t, id),
                reference,
                at: xy(t, id, "at").unwrap_or((0.0, 0.0)),
                rotation: at_node.and_then(|a| t.arg_num(a, 2)).unwrap_or(0.0),
                layer: t
                    .find(id, "layer")
                    .and_then(|n| t.arg_text(n, 0))
                    .map(|s| s.to_string())
                    .unwrap_or_default(),
                locked: node_locked(t, id),
                pads,
            });
        }
        out
    }

    /// Named nets with their pad counts. Nets known only from the table, tracks
    /// or zones appear with a count of zero.
    pub fn nets(&self) -> BTreeMap<String, usize> {
        let t = &self.tree;
        let mut m: BTreeMap<String, usize> = BTreeMap::new();
        for name in self.net_table.values() {
            if !name.is_empty() {
                m.entry(name.clone()).or_insert(0);
            }
        }
        for &id in &self.top {
            match t.head(id) {
                "footprint" => {
                    for p in t.find_all(id, "pad") {
                        if let Some(n) = self.node_net(p).0 {
                            *m.entry(n).or_insert(0) += 1;
                        }
                    }
                }
                "segment" | "arc" | "via" | "zone" => {
                    if let Some(n) = self.node_net(id).0 {
                        m.entry(n).or_insert(0);
                    }
                }
                _ => {}
            }
        }
        m
    }

    pub fn known_nets(&self) -> HashSet<String> {
        self.nets().into_keys().collect()
    }

    /// Bounding box of everything on Edge.Cuts, including footprint outlines.
    pub fn outline_bbox(&self) -> Option<(f64, f64, f64, f64)> {
        let t = &self.tree;
        let mut pts: Vec<(f64, f64)> = Vec::new();
        for &id in &self.top {
            if t.kind(id) != Kind::List {
                continue;
            }
            let head = t.head(id);
            if head.starts_with("gr_") {
                pts.extend(self.edge_points(id));
            } else if head == "footprint" {
                let at = xy(t, id, "at").unwrap_or((0.0, 0.0));
                let rot = t.find(id, "at").and_then(|a| t.arg_num(a, 2)).unwrap_or(0.0);
                for c in t.children(id) {
                    if t.kind(c) == Kind::List && t.head(c).starts_with("fp_") {
                        pts.extend(self.edge_points(c).into_iter().map(|p| local_to_board(p, at, rot)));
                    }
                }
            }
        }
        let first = *pts.first()?;
        let mut b = (first.0, first.1, first.0, first.1);
        for p in pts {
            b = (b.0.min(p.0), b.1.min(p.1), b.2.max(p.0), b.3.max(p.1));
        }
        Some(b)
    }

    fn edge_points(&self, id: Id) -> Vec<(f64, f64)> {
        let t = &self.tree;
        let on_edge = t.find(id, "layer").and_then(|n| t.arg_text(n, 0)).as_deref() == Some("Edge.Cuts");
        if !on_edge {
            return vec![];
        }
        let head = t.head(id);
        let shape = &head[3..];
        let mut pts = vec![];
        match shape {
            "line" => pts.extend([xy(t, id, "start"), xy(t, id, "end")].into_iter().flatten()),
            "rect" => {
                if let (Some(a), Some(b)) = (xy(t, id, "start"), xy(t, id, "end")) {
                    pts.extend([a, b, (a.0, b.1), (b.0, a.1)]);
                }
            }
            "circle" => {
                if let (Some(c), Some(e)) = (xy(t, id, "center"), xy(t, id, "end")) {
                    let r = ((c.0 - e.0).powi(2) + (c.1 - e.1).powi(2)).sqrt();
                    pts.extend([(c.0 - r, c.1), (c.0 + r, c.1), (c.0, c.1 - r), (c.0, c.1 + r)]);
                }
            }
            "arc" => {
                if let (Some(a), Some(m), Some(b)) = (xy(t, id, "start"), xy(t, id, "mid"), xy(t, id, "end")) {
                    pts.extend(arc_extremes(a, m, b));
                } else {
                    pts.extend([xy(t, id, "start"), xy(t, id, "end")].into_iter().flatten());
                }
            }
            _ => {
                if let Some(ps) = t.find(id, "pts") {
                    for p in t.find_all(ps, "xy") {
                        if let (Some(x), Some(y)) = (t.arg_num(p, 0), t.arg_num(p, 1)) {
                            pts.push((x, y));
                        }
                    }
                }
            }
        }
        pts
    }
}

/// Endpoints and any axis extreme lying on the arc through a, m, b.
pub fn arc_extremes(a: (f64, f64), m: (f64, f64), b: (f64, f64)) -> Vec<(f64, f64)> {
    let mut pts = vec![a, m, b];
    let d = 2.0 * (a.0 * (m.1 - b.1) + m.0 * (b.1 - a.1) + b.0 * (a.1 - m.1));
    if d.abs() < 1e-12 {
        return pts;
    }
    let sq = |p: (f64, f64)| p.0 * p.0 + p.1 * p.1;
    let ux = (sq(a) * (m.1 - b.1) + sq(m) * (b.1 - a.1) + sq(b) * (a.1 - m.1)) / d;
    let uy = (sq(a) * (b.0 - m.0) + sq(m) * (a.0 - b.0) + sq(b) * (m.0 - a.0)) / d;
    let r = ((a.0 - ux).powi(2) + (a.1 - uy).powi(2)).sqrt();
    let ang = |p: (f64, f64)| (p.1 - uy).atan2(p.0 - ux);
    let tau = std::f64::consts::TAU;
    let norm = |x: f64| x.rem_euclid(tau);
    let (a0, m0, b0) = (ang(a), ang(m), ang(b));
    // Travel from a to b in the direction that passes through m.
    let ccw = norm(m0 - a0) <= norm(b0 - a0);
    for (i, p) in [(ux + r, uy), (ux, uy + r), (ux - r, uy), (ux, uy - r)].into_iter().enumerate() {
        let q = i as f64 * std::f64::consts::FRAC_PI_2;
        let on = if ccw { norm(q - a0) <= norm(b0 - a0) } else { norm(a0 - q) <= norm(a0 - b0) };
        if on {
            pts.push(p);
        }
    }
    pts
}

/// Digest of a track or via's geometry and net, independent of uuid and text
/// formatting, so a router's rewritten copy of an item can be recognised.
pub fn geometry_digest(tr: &Track) -> String {
    let net = tr.net.as_deref().unwrap_or("");
    let p = |v: (f64, f64)| format!("{},{}", to_nm(v.0), to_nm(v.1));
    let canonical = match tr.kind {
        TrackKind::Via => {
            let mut l = tr.layers.clone();
            l.sort();
            format!("via|{net}|{}|{}|{}|{}", p(tr.start), to_nm(tr.width), to_nm(tr.drill), l.join("+"))
        }
        TrackKind::Segment => {
            let (mut a, mut b) = (p(tr.start), p(tr.end));
            if a > b {
                std::mem::swap(&mut a, &mut b);
            }
            format!("segment|{net}|{}|{a}|{b}|{}", tr.layers.join("+"), to_nm(tr.width))
        }
        TrackKind::Arc => {
            let (mut a, mut b) = (p(tr.start), p(tr.end));
            if a > b {
                std::mem::swap(&mut a, &mut b);
            }
            let m = p(tr.mid.unwrap_or((0.0, 0.0)));
            format!("arc|{net}|{}|{a}|{m}|{b}|{}", tr.layers.join("+"), to_nm(tr.width))
        }
    };
    sha256_hex(canonical.as_bytes())
}

pub fn track_json(tr: &Track) -> Value {
    let mut v = json!({
        "uuid": tr.uuid,
        "kind": tr.kind.as_str(),
        "net": tr.net,
        "locked": tr.locked,
    });
    let o = v.as_object_mut().expect("object literal");
    if tr.net.is_none() {
        if let Some(n) = tr.net_number {
            o.insert("net_number".into(), json!(n));
        }
    }
    let pt = |p: (f64, f64)| json!({"x": p.0, "y": p.1});
    if tr.kind == TrackKind::Via {
        o.insert("layers".into(), json!(tr.layers));
        o.insert("at".into(), pt(tr.start));
        o.insert("size".into(), json!(tr.width));
        o.insert("drill".into(), json!(tr.drill));
        o.insert("via_type".into(), json!(tr.via_type));
    } else {
        o.insert("layer".into(), json!(tr.layers.first()));
        o.insert("start".into(), pt(tr.start));
        if let Some(m) = tr.mid {
            o.insert("mid".into(), pt(m));
        }
        o.insert("end".into(), pt(tr.end));
        o.insert("width".into(), json!(tr.width));
    }
    v
}

pub fn footprint_json(f: &Footprint) -> Value {
    let side = match f.layer.as_str() {
        "F.Cu" => "front",
        "B.Cu" => "back",
        _ => "unknown",
    };
    json!({
        "reference": f.reference,
        "footprint": f.id,
        "uuid": f.uuid,
        "position": {"x": f.at.0, "y": f.at.1},
        "rotation": f.rotation,
        "side": side,
        "locked": f.locked,
        "pads": f.pads.iter().map(|p| json!({"number": p.number, "net": p.net})).collect::<Vec<_>>(),
    })
}

/// Summary counts, reported from the parse.
pub fn summary_json(b: &Board) -> Value {
    let t = &b.tree;
    let (mut fp, mut pads, mut seg, mut arc, mut via, mut zones, mut fp_zones) = (0u64, 0u64, 0u64, 0u64, 0u64, 0u64, 0u64);
    let mut locked: BTreeMap<String, u64> = BTreeMap::new();
    for &id in &b.top {
        if t.kind(id) != Kind::List {
            continue;
        }
        let head = t.head(id);
        match head {
            "footprint" => {
                fp += 1;
                pads += t.find_all(id, "pad").count() as u64;
                fp_zones += t.find_all(id, "zone").count() as u64;
            }
            "segment" => seg += 1,
            "arc" => arc += 1,
            "via" => via += 1,
            "zone" => zones += 1,
            _ => {}
        }
        if node_locked(t, id) {
            *locked.entry(head.to_string()).or_insert(0) += 1;
        }
    }
    let nets = b.nets();
    let bbox = b.outline_bbox().map(|o| {
        json!({"min_x": o.0, "min_y": o.1, "max_x": o.2, "max_y": o.3, "width": o.2 - o.0, "height": o.3 - o.1})
    });
    json!({
        "revision": b.revision(),
        "layers": b.layers.iter().map(|l| l.name.clone()).collect::<Vec<_>>(),
        "copper_layer_count": b.copper_layers().len(),
        "outline_bbox": bbox,
        "counts": {
            "footprints": fp,
            "pads": pads,
            "segments": seg,
            "arcs": arc,
            "vias": via,
            "zones": zones,
            "footprint_zones": fp_zones,
            "nets": nets.len(),
            "nets_with_pads": nets.values().filter(|c| **c > 0).count(),
            "locked_items": locked.values().sum::<u64>(),
        },
        "locked_by_kind": locked,
    })
}

pub fn stackup_json(b: &Board) -> Option<Value> {
    let t = &b.tree;
    let setup = t.find(t.root, "setup")?;
    let st = t.find(setup, "stackup")?;
    let mut out = vec![];
    for l in t.find_all(st, "layer") {
        let num = |h: &str| t.find(l, h).and_then(|n| t.arg_num(n, 0));
        out.push(json!({
            "name": t.arg_text(l, 0).map(|s| s.to_string()),
            "type": t.find(l, "type").and_then(|n| t.arg_text(n, 0)).map(|s| s.to_string()),
            "thickness": num("thickness"),
            "epsilon_r": num("epsilon_r"),
        }));
    }
    Some(json!(out))
}

#[cfg(test)]
mod tests {
    use super::*;

    const NUMBERED: &str = r#"(kicad_pcb (version 20221018)
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "GND")
  (net 2 "A\"B")
  (footprint "R" locked (layer "F.Cu") (at 1 2 90)
    (fp_text reference "R1" (at 0 0))
    (fp_text value "locked" (at 0 0))
    (pad "1" smd rect (at 0 0) (net 1 "GND"))
    (pad "2" smd rect (at 1 0) (net 2 "A\"B"))
    (pad "3" smd rect (at 2 0)))
  (gr_arc (start 0 0) (mid 5 5) (end 10 0) (layer "Edge.Cuts"))
  (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 2) (tstamp aaa))
  (via blind locked (at 3 3) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (tstamp bbb))
)"#;

    #[test]
    fn numbered_form_resolves_names() {
        let b = Board::parse(NUMBERED.as_bytes().to_vec()).unwrap();
        assert_eq!(b.net_form(), NetForm::Numbered);
        let t = b.tracks();
        assert_eq!(t[0].net.as_deref(), Some("A\"B"));
        assert_eq!(t[0].uuid.as_deref(), Some("aaa"));
        assert!(!t[0].locked);
        assert!(t[1].locked);
        assert_eq!(t[1].via_type, "blind");
        let f = &b.footprints()[0];
        assert!(f.locked);
        assert_eq!(f.reference.as_deref(), Some("R1"));
        assert_eq!(f.pads[2].net, None);
        let n = b.nets();
        assert_eq!(n["GND"], 1);
        assert_eq!(n.len(), 2);
    }

    #[test]
    fn named_net_that_looks_like_a_number_stays_a_name() {
        let b = Board::parse(br#"(kicad_pcb (segment (start 0 0) (end 1 1) (width 1) (layer "F.Cu") (net "3")))"#.to_vec()).unwrap();
        assert_eq!(b.net_form(), NetForm::Named);
        assert_eq!(b.tracks()[0].net.as_deref(), Some("3"));
    }

    #[test]
    fn locked_text_elsewhere_is_not_a_lock() {
        let s = r#"(kicad_pcb (footprint "locked" (layer "F.Cu") (at 0 0)
          (property "atopile_native_layout" "locked") (fp_text reference "U" (at 0 0) (unlocked yes))))"#;
        let b = Board::parse(s.as_bytes().to_vec()).unwrap();
        assert!(!b.footprints()[0].locked);
    }

    #[test]
    fn arc_bbox_includes_bulge() {
        let b = Board::parse(NUMBERED.as_bytes().to_vec()).unwrap();
        let o = b.outline_bbox().unwrap();
        assert!((o.3 - 5.0).abs() < 1e-9 && o.1.abs() < 1e-9, "{o:?}");
        // A three-quarter arc reaches past both of its endpoints.
        let p = arc_extremes((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0));
        let min_y = p.iter().map(|q| q.1).fold(f64::MAX, f64::min);
        assert!((min_y + 1.0).abs() < 1e-9, "{p:?}");
    }

    #[test]
    fn arcs_are_read_with_their_midpoint() {
        let s = r#"(kicad_pcb (layers (0 "F.Cu" signal))
          (arc (start 0 0) (mid 1 1) (end 2 0) (width 0.2) (locked yes) (layer "F.Cu") (net "A") (uuid "x")))"#;
        let b = Board::parse(s.as_bytes().to_vec()).unwrap();
        let t = &b.tracks()[0];
        assert_eq!(t.kind, TrackKind::Arc);
        assert!(t.locked);
        let j = track_json(t);
        assert_eq!(j["mid"]["x"].as_f64(), Some(1.0));
        assert_eq!(j["layer"], "F.Cu");
        assert_eq!(j["width"].as_f64(), Some(0.2));
        assert_eq!(summary_json(&b)["counts"]["arcs"], 1);
    }

    #[test]
    fn geometry_digest_ignores_direction_and_uuid() {
        let s = r#"(kicad_pcb
          (segment (start 0 0) (end 1 0.5) (width 0.2) (layer "F.Cu") (net "A") (uuid "x"))
          (segment (end 0 0) (start 1 0.5) (width 0.20) (locked yes) (layer "F.Cu") (net "A") (uuid "y")))"#;
        let b = Board::parse(s.as_bytes().to_vec()).unwrap();
        let t = b.tracks();
        assert_eq!(geometry_digest(&t[0]), geometry_digest(&t[1]));
    }
}
