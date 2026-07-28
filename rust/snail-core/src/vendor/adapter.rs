//! VendorAdapter trait — the vendor-neutral ↔ wire boundary (port of `snail.vendor.base`).
//!
//! The adapter is pure translation: it serializes neutral `Item`/setup/tool results to a vendor's
//! wire shape, and parses vendor wire messages into neutral [`ParsedEvent`]s. It owns no socket —
//! the live connection wraps a socket and *uses* an adapter. Keeping translation socket-free is
//! what makes it testable without a vendor key (see [`super::mock::MockVendorAdapter`]).

use serde_json::Value;

use crate::context::Item;

use super::capabilities::VendorCapabilities;
use super::events::ParsedEvent;
use super::media::{MediaChunk, RealtimeControl};
use super::params::SetupParam;

/// Translate between the neutral surface and one vendor's wire format. Methods take `&self` — a
/// recording test adapter uses interior mutability.
pub trait VendorAdapter {
    fn name(&self) -> &str;
    fn capabilities(&self) -> &VendorCapabilities;

    /// Serialize the static identity to the vendor's setup/config message.
    fn build_setup(&self, setup: &SetupParam) -> Value;
    /// Serialize one neutral `Item` to a vendor content turn (down-converting SYSTEM as needed).
    fn serialize_item(&self, item: &Item) -> Value;
    /// Serialize projected history for injection on join (before the first turn).
    fn serialize_history(&self, items: &[Item]) -> Vec<Value> {
        items.iter().map(|i| self.serialize_item(i)).collect()
    }

    /// Serialize a streaming multimodal chunk for the vendor's realtime channel.
    fn serialize_realtime(&self, chunk: &MediaChunk) -> Value;
    /// Serialize an out-of-band realtime control marker (activity/stream-end).
    fn serialize_realtime_control(&self, control: RealtimeControl) -> Value;
    /// Serialize ordered content turns (`complete` maps to `turn_complete`).
    fn serialize_turns(&self, items: &[Item], complete: bool) -> Value;
    /// Serialize a tool result to the vendor's function-response shape.
    fn serialize_tool_result(
        &self,
        call_id: &str,
        name: &str,
        content: &str,
        meta: Option<&Value>,
    ) -> Value;

    /// Parse one raw vendor wire message into zero or more neutral events.
    fn parse_event(&self, raw: &Value) -> Vec<ParsedEvent>;

    /// Extract agent output audio (PCM16 mono bytes) from a raw message, if any. Default `None`
    /// for adapters whose audio arrives out-of-band; the Gemini adapter pulls it from `modelTurn`.
    fn extract_output_audio(&self, _raw: &Value) -> Option<Vec<u8>> {
        None
    }
}
