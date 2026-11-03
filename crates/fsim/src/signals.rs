//! Signal identities and signal vectors.
//!
//! Every concrete signal (type, name, quality) gets a dense id. Ids follow the game's internal
//! signal order (captured from Factorio by `harness/run.py --signals`), so sorting by id
//! reproduces the order the game lists signals in and breaks selector ties the same way.

use std::collections::HashMap;

/// The game's signal order for vanilla 2.1 (base mod, normal + quality-unknown).
const VANILLA_ORDER: &str = include_str!("../../../conformance/signal_order.json");
/// The same with Space Age (space-age, quality, elevated-rails): six qualities per signal.
const DLC_ORDER: &str = include_str!("../../../conformance/signal_order_dlc.json");

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct SignalKey {
    pub ty: String,
    pub name: String,
    pub quality: String,
}

impl SignalKey {
    pub fn new(ty: &str, name: &str, quality: &str) -> Self {
        SignalKey { ty: ty.to_string(), name: name.to_string(), quality: quality.to_string() }
    }

    /// Short display form: `signal-A`, `iron-plate[item]`, `signal-A@quality-unknown`.
    pub fn short(&self) -> String {
        let mut s = self.name.clone();
        if self.ty != "virtual" {
            s.push_str(&format!("[{}]", self.ty));
        }
        if self.quality != "normal" {
            s.push_str(&format!("@{}", self.quality));
        }
        s
    }
}

pub struct SignalTable {
    keys: Vec<SignalKey>,
    index: HashMap<SignalKey, u32>,
    /// Ids below this come from the game's order; ids at or above it were added by blueprints.
    pub known: usize,
}

impl SignalTable {
    pub fn vanilla() -> Self {
        Self::from_order(VANILLA_ORDER)
    }

    pub fn dlc() -> Self {
        Self::from_order(DLC_ORDER)
    }

    fn from_order(order: &str) -> Self {
        let list: Vec<Vec<String>> = serde_json::from_str(order).expect("signal order");
        let mut t = SignalTable { keys: Vec::new(), index: HashMap::new(), known: 0 };
        for k in list {
            t.intern(SignalKey::new(&k[0], &k[1], &k[2]));
        }
        t.known = t.keys.len();
        t
    }

    pub fn intern(&mut self, key: SignalKey) -> u32 {
        if let Some(&id) = self.index.get(&key) {
            return id;
        }
        let id = self.keys.len() as u32;
        self.index.insert(key.clone(), id);
        self.keys.push(key);
        id
    }

    pub fn find(&self, key: &SignalKey) -> Option<u32> {
        self.index.get(key).copied()
    }

    pub fn key(&self, id: u32) -> &SignalKey {
        &self.keys[id as usize]
    }

    pub fn len(&self) -> usize {
        self.keys.len()
    }

    pub fn is_empty(&self) -> bool {
        self.keys.is_empty()
    }

    pub fn format(&self, v: &SigVec) -> String {
        v.iter().map(|&(id, c)| format!("{}={}", self.key(id).short(), c)).collect::<Vec<_>>().join(" ")
    }
}

/// Quality level (Space Age): normal 0 .. legendary 5. quality-unknown passes a `>= rare` quality
/// filter in game (`quality_ops`), so it ranks above legendary here.
pub fn quality_level(q: &str) -> Option<i32> {
    match q {
        "normal" => Some(0),
        "uncommon" => Some(1),
        "rare" => Some(2),
        "epic" => Some(3),
        "legendary" => Some(5),
        "quality-unknown" => Some(6),
        _ => None,
    }
}

/// A set of signal values, sorted by id, with no zero entries. This is what a wire carries.
pub type SigVec = Vec<(u32, i32)>;

pub fn get(v: &[(u32, i32)], id: u32) -> i32 {
    match v.binary_search_by_key(&id, |e| e.0) {
        Ok(i) => v[i].1,
        Err(_) => 0,
    }
}

/// Lane-wise wrapping sum of two vectors (what joining two wires onto one network does).
pub fn add(a: &[(u32, i32)], b: &[(u32, i32)]) -> SigVec {
    if a.is_empty() {
        return b.to_vec();
    }
    if b.is_empty() {
        return a.to_vec();
    }
    let mut out = Vec::with_capacity(a.len() + b.len());
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        let (ia, va) = a[i];
        let (ib, vb) = b[j];
        if ia < ib {
            out.push(a[i]);
            i += 1;
        } else if ib < ia {
            out.push(b[j]);
            j += 1;
        } else {
            let v = va.wrapping_add(vb);
            if v != 0 {
                out.push((ia, v));
            }
            i += 1;
            j += 1;
        }
    }
    out.extend_from_slice(&a[i..]);
    out.extend_from_slice(&b[j..]);
    out
}

/// Accumulates many (id, value) contributions in any order, then produces a SigVec.
#[derive(Default)]
pub struct Accum {
    map: HashMap<u32, i32>,
}

impl Accum {
    pub fn add(&mut self, id: u32, v: i32) {
        if v != 0 {
            let e = self.map.entry(id).or_insert(0);
            *e = e.wrapping_add(v);
        }
    }

    pub fn finish(self) -> SigVec {
        let mut v: SigVec = self.map.into_iter().filter(|e| e.1 != 0).collect();
        v.sort_unstable_by_key(|e| e.0);
        v
    }
}
