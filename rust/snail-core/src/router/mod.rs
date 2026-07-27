//! Output routing primitives — the token-guarded [`gate::OutputGate`] (GATE 2). The full
//! Router/policy layer stays in Python for now (migrated in a later phase).

pub mod gate;

pub use gate::{GateStats, OutputGate};
