//! The uniform tool reply of section 2.
use serde_json::{json, Map, Value};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Outcome {
    Complete,
    Partial,
    Refused,
    Uncertain,
    Unavailable,
}

impl Outcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Outcome::Complete => "complete",
            Outcome::Partial => "partial",
            Outcome::Refused => "refused",
            Outcome::Uncertain => "uncertain",
            Outcome::Unavailable => "unavailable",
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Source {
    File,
    Cli,
    None,
}

#[derive(Clone, Debug)]
pub struct Reply {
    pub outcome: Outcome,
    pub source: Source,
    pub target: Option<Box<str>>,
    pub revision_before: Option<Box<str>>,
    pub revision_after: Option<Box<str>>,
    pub result: Value,
    pub reason: Option<Box<str>>,
    pub detail: Option<Box<str>>,
}

impl Reply {
    fn base(outcome: Outcome, source: Source, result: Value) -> Reply {
        Reply { outcome, source, target: None, revision_before: None, revision_after: None, result, reason: None, detail: None }
    }

    pub fn complete(source: Source, result: Value) -> Reply {
        Reply::base(Outcome::Complete, source, result)
    }

    pub fn partial(source: Source, reason: &str, detail: impl Into<String>, result: Value) -> Reply {
        Reply::base(Outcome::Partial, source, result).why(reason, detail)
    }

    /// A refusal. The sentence stating that nothing changed is added here, once.
    pub fn refused(source: Source, reason: &str, detail: impl Into<String>) -> Reply {
        let d = format!("{} Nothing was changed.", detail.into());
        Reply::base(Outcome::Refused, source, json!({"changed": false})).why(reason, d)
    }

    pub fn uncertain(source: Source, reason: &str, detail: impl Into<String>) -> Reply {
        Reply::base(Outcome::Uncertain, source, json!({})).why(reason, detail)
    }

    pub fn unavailable(source: Source, reason: &str, detail: impl Into<String>) -> Reply {
        Reply::base(Outcome::Unavailable, source, json!({})).why(reason, detail)
    }

    fn why(mut self, reason: &str, detail: impl Into<String>) -> Reply {
        self.reason = Some(reason.into());
        self.detail = Some(detail.into().into_boxed_str());
        self
    }

    pub fn target(mut self, t: impl AsRef<std::path::Path>) -> Reply {
        self.target = Some(t.as_ref().to_string_lossy().into());
        self
    }

    pub fn revs(mut self, before: Option<&str>, after: Option<&str>) -> Reply {
        self.revision_before = before.map(Into::into);
        self.revision_after = after.map(Into::into);
        self
    }

    /// Merge object fields into `result`.
    pub fn with(mut self, extra: Value) -> Reply {
        if let (Some(r), Value::Object(e)) = (self.result.as_object_mut(), extra) {
            r.extend(e);
        }
        self
    }

    pub fn source_cli(mut self) -> Reply {
        self.source = Source::Cli;
        self
    }

    pub fn is_error(&self) -> bool {
        matches!(self.outcome, Outcome::Refused | Outcome::Uncertain | Outcome::Unavailable)
    }

    pub fn to_json(&self) -> Value {
        let mut m = Map::new();
        m.insert("outcome".into(), json!(self.outcome.as_str()));
        m.insert(
            "source".into(),
            json!(match self.source {
                Source::File => "file",
                Source::Cli => "cli",
                Source::None => "none",
            }),
        );
        if let Some(t) = &self.target {
            m.insert("target".into(), json!(t.as_ref()));
        }
        if let Some(r) = &self.revision_before {
            m.insert("revision_before".into(), json!(r.as_ref()));
        }
        if let Some(r) = &self.revision_after {
            m.insert("revision_after".into(), json!(r.as_ref()));
        }
        m.insert("result".into(), self.result.clone());
        if let Some(r) = &self.reason {
            m.insert("reason".into(), json!(r.as_ref()));
        }
        if let Some(d) = &self.detail {
            m.insert("detail".into(), json!(d.as_ref()));
        }
        Value::Object(m)
    }
}
