//! Vendor-neutral surface (port of `snail.vendor`): capability descriptors, neutral connection
//! params, parsed wire events, and realtime media. The concrete adapters (Gemini Live wire
//! protocol) land with the tokio/transport phase; this is the language every adapter speaks.

pub mod capabilities;
pub mod events;
pub mod media;
pub mod params;

pub use capabilities::{Backend, VendorCapabilities};
pub use events::ParsedEvent;
pub use media::{MediaChunk, MediaKind, RealtimeControl};
pub use params::{InputSource, JoinContext, ResponseModality, SetupParam, ToolSpec};
