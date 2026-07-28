//! Vendor-neutral context model (port of `snail.context`): the append-only event log, its
//! canonical event/item schema, and declarative projections to `Vec<Item>`.

pub mod events;
pub mod log;
pub mod projection;

pub use events::{Event, EventType, Item, Role};
pub use log::EventLog;
pub use projection::Projection;
