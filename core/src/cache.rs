//! LRU of fixed-size file blocks, bounded in bytes.

use std::sync::Arc;

use bytes::Bytes;
use lru::LruCache;

pub const BLOCK_SIZE: u64 = 256 * 1024;

/// mtime has whole-second resolution over SFTP v3, so same-size rewrites inside
/// a second are told apart by ``generation``, which change events bump.
#[derive(Clone, Debug, Hash, PartialEq, Eq)]
pub struct FileKey {
    pub path: Vec<u8>,
    pub mtime: u32,
    pub generation: u64,
    pub size: u64,
}

pub type BlockId = (Arc<FileKey>, u64);

pub struct BlockCache {
    blocks: LruCache<BlockId, Bytes>,
    used: usize,
    limit: usize,
}

impl BlockCache {
    pub fn new(limit: usize) -> Self {
        BlockCache { blocks: LruCache::unbounded(), used: 0, limit }
    }

    pub fn get(&mut self, id: &BlockId) -> Option<Bytes> {
        self.blocks.get(id).cloned()
    }

    pub fn contains(&self, id: &BlockId) -> bool {
        self.blocks.contains(id)
    }

    pub fn put(&mut self, id: BlockId, data: Bytes) {
        self.used += data.len();
        if let Some(old) = self.blocks.put(id, data) {
            self.used -= old.len();
        }
        while self.used > self.limit {
            match self.blocks.pop_lru() {
                Some((_, evicted)) => self.used -= evicted.len(),
                None => break,
            }
        }
    }

    pub fn clear(&mut self) {
        self.blocks.clear();
        self.used = 0;
    }
}
