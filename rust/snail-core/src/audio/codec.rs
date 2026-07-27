//! Codec — client-leg encode/decode (port of `snail.audio.codec`).
//!
//! The codec is the client-leg latency/bandwidth lever. Interior is always 48k int16 mono, so
//! a codec here neither resamples nor changes channel count; it only (de)compresses. [`PcmCodec`]
//! is the v0 default: int16 mono ⇄ PCM16 little-endian bytes — the raw wire form the transport
//! uses today (no dependency). An opus codec can satisfy the same trait later.

/// Client-leg (de)compression. Samples are 48k int16 mono; bytes are the wire form.
pub trait AudioCodec {
    fn encode(&mut self, samples: &[i16]) -> Vec<u8>;
    fn decode(&mut self, data: &[u8]) -> Vec<i16>;
}

/// Passthrough codec: int16 mono ⇄ PCM16 little-endian bytes (v0 default).
#[derive(Debug, Default, Clone, Copy)]
pub struct PcmCodec;

impl AudioCodec for PcmCodec {
    fn encode(&mut self, samples: &[i16]) -> Vec<u8> {
        let mut out = Vec::with_capacity(samples.len() * 2);
        for &s in samples {
            out.extend_from_slice(&s.to_le_bytes());
        }
        out
    }

    fn decode(&mut self, data: &[u8]) -> Vec<i16> {
        // Truncate a trailing odd byte (matches numpy frombuffer on aligned PCM16LE input).
        data.chunks_exact(2)
            .map(|b| i16::from_le_bytes([b[0], b[1]]))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pcm_roundtrip_le() {
        let mut c = PcmCodec;
        let samples = [0i16, 1, -1, 32767, -32768, 256];
        let bytes = c.encode(&samples);
        assert_eq!(&bytes[0..2], &[0, 0]);
        assert_eq!(&bytes[2..4], &[1, 0]); // little-endian
        assert_eq!(&bytes[4..6], &[255, 255]); // -1
        assert_eq!(c.decode(&bytes), samples.to_vec());
    }
}
