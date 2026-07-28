//! ToolRegistry — the static catalog (port of `snail.tools.registry`).
//!
//! `name → Tool`. Session/global, reusable across agents ("what tools exist"). Distinct from the
//! live in-flight [`crate::registry::ToolCallRegistry`] ("what calls are happening now").

use std::collections::HashMap;

use crate::vendor::params::ToolSpec;

use super::tool::Tool;

/// A catalog of tools, keyed by name (insertion order preserved for stable spec output).
#[derive(Default)]
pub struct ToolRegistry {
    order: Vec<String>,
    tools: HashMap<String, Tool>,
}

impl ToolRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register a tool. Panics on a duplicate name (a wiring bug).
    pub fn register(&mut self, tool: Tool) {
        assert!(
            !self.tools.contains_key(&tool.name),
            "tool {:?} already registered",
            tool.name
        );
        self.order.push(tool.name.clone());
        self.tools.insert(tool.name.clone(), tool);
    }

    pub fn get(&self, name: &str) -> Option<&Tool> {
        self.tools.get(name)
    }

    /// Vendor-neutral declarations for exposure. `names = None` → the whole catalog; otherwise the
    /// per-agent subset (unknown names skipped).
    pub fn specs(&self, names: Option<&[String]>) -> Vec<ToolSpec> {
        match names {
            None => self.order.iter().map(|n| self.tools[n].to_spec()).collect(),
            Some(ns) => ns
                .iter()
                .filter_map(|n| self.tools.get(n))
                .map(Tool::to_spec)
                .collect(),
        }
    }

    pub fn names(&self) -> &[String] {
        &self.order
    }
    pub fn contains(&self, name: &str) -> bool {
        self.tools.contains_key(name)
    }
    pub fn len(&self) -> usize {
        self.tools.len()
    }
    pub fn is_empty(&self) -> bool {
        self.tools.is_empty()
    }
}
