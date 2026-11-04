//! Block fetching: single flight per block, priorities, cancellation.

use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap, HashSet};
use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use bytes::Bytes;
use tokio::sync::watch;
use tokio::task::AbortHandle;

use crate::cache::{BlockCache, BlockId, FileKey, BLOCK_SIZE};
use crate::error::{Result, RvError};

const DEMAND: u8 = 0;
const READ_AHEAD: u8 = 1;

pub type ReadBlock =
    Arc<dyn Fn(Vec<u8>, u64, u64) -> Pin<Box<dyn Future<Output = Result<Bytes>> + Send>> + Send + Sync>;

type Outcome = Option<Result<Bytes>>;

struct Job {
    block: BlockId,
    sender: watch::Sender<Outcome>,
    refs: usize,
    priority: u8,
    task: Option<AbortHandle>,
}

#[derive(Default)]
struct State {
    jobs: HashMap<u64, Job>,
    by_block: HashMap<BlockId, u64>,
    queue: BinaryHeap<Reverse<(u8, u64, u64)>>,
    owned: HashMap<u64, HashSet<u64>>,
    running: usize,
    next: u64,
}

pub struct Scheduler {
    state: Mutex<State>,
    pub cache: Mutex<BlockCache>,
    read_block: ReadBlock,
    workers: usize,
    owners: AtomicU64,
    pub reads_issued: AtomicU64,
}

/// Drops the read if this was the last party interested in it.
struct Interest {
    scheduler: Arc<Scheduler>,
    job: u64,
}

impl Drop for Interest {
    fn drop(&mut self) {
        let mut state = self.scheduler.state.lock().unwrap();
        Scheduler::release(&self.scheduler, &mut state, self.job);
    }
}

/// Read-ahead held by one consumer; ends with it.
pub struct Owner {
    scheduler: Arc<Scheduler>,
    pub id: u64,
}

impl Drop for Owner {
    fn drop(&mut self) {
        self.scheduler.cancel_prefetch(self.id);
    }
}

impl Scheduler {
    pub fn new(cache_bytes: usize, read_block: ReadBlock, workers: usize) -> Arc<Self> {
        Arc::new(Scheduler {
            state: Mutex::new(State::default()),
            cache: Mutex::new(BlockCache::new(cache_bytes)),
            read_block,
            workers,
            owners: AtomicU64::new(1),
            reads_issued: AtomicU64::new(0),
        })
    }

    pub fn owner(self: &Arc<Self>) -> Owner {
        Owner { scheduler: self.clone(), id: self.owners.fetch_add(1, Ordering::Relaxed) }
    }

    pub async fn get(self: &Arc<Self>, key: &Arc<FileKey>, index: u64) -> Result<Bytes> {
        let block = (key.clone(), index);
        if let Some(data) = self.cache.lock().unwrap().get(&block) {
            return Ok(data);
        }
        let (job, mut receiver) = {
            let mut state = self.state.lock().unwrap();
            let job = self.submit(&mut state, block, DEMAND);
            let entry = state.jobs.get_mut(&job).expect("just submitted");
            entry.refs += 1;
            let receiver = entry.sender.subscribe();
            self.pump(&mut state);
            (job, receiver)
        };
        let _interest = Interest { scheduler: self.clone(), job };
        loop {
            if let Some(outcome) = receiver.borrow_and_update().as_ref() {
                return outcome.clone();
            }
            if receiver.changed().await.is_err() {
                return Err(RvError::Cancelled);
            }
        }
    }

    pub fn prefetch(self: &Arc<Self>, owner: &Owner, key: &Arc<FileKey>, first: u64, count: u64) {
        if key.size == 0 {
            return;
        }
        let last = (key.size - 1) / BLOCK_SIZE;
        let mut state = self.state.lock().unwrap();
        let state = &mut *state;
        let mut held = state.owned.remove(&owner.id).unwrap_or_default();
        held.retain(|job| state.jobs.contains_key(job));
        for index in first..(first + count).min(last + 1) {
            let block = (key.clone(), index);
            if self.cache.lock().unwrap().contains(&block) {
                continue;
            }
            let job = self.submit(state, block, READ_AHEAD);
            if held.insert(job) {
                state.jobs.get_mut(&job).expect("just submitted").refs += 1;
            }
        }
        state.owned.insert(owner.id, held);
        self.pump(state);
    }

    pub fn cancel_prefetch(self: &Arc<Self>, owner: u64) {
        let mut state = self.state.lock().unwrap();
        for job in state.owned.remove(&owner).unwrap_or_default() {
            Self::release(self, &mut state, job);
        }
    }

    fn submit(&self, state: &mut State, block: BlockId, priority: u8) -> u64 {
        if let Some(&id) = state.by_block.get(&block) {
            let next = state.next;
            let job = state.jobs.get_mut(&id).expect("indexed job exists");
            if priority < job.priority && job.task.is_none() {
                job.priority = priority;
                state.next += 1;
                state.queue.push(Reverse((priority, next, id)));
            }
            return id;
        }
        let id = state.next;
        state.next += 1;
        let (sender, _) = watch::channel(None);
        state.by_block.insert(block.clone(), id);
        state.jobs.insert(id, Job { block, sender, refs: 0, priority, task: None });
        state.queue.push(Reverse((priority, id, id)));
        id
    }

    fn release(this: &Arc<Self>, state: &mut State, id: u64) {
        let Some(job) = state.jobs.get_mut(&id) else { return };
        job.refs = job.refs.saturating_sub(1);
        if job.refs > 0 {
            return;
        }
        // Nobody wants it any more: stop the read, or never start it.
        let job = state.jobs.remove(&id).expect("just looked up");
        state.by_block.remove(&job.block);
        if let Some(task) = job.task {
            task.abort();
            state.running -= 1;
            this.pump(state);
        }
    }

    fn pump(self: &Arc<Self>, state: &mut State) {
        while state.running < self.workers {
            let Some(Reverse((_, _, id))) = state.queue.pop() else { return };
            let Some(job) = state.jobs.get(&id) else { continue };
            if job.task.is_some() {
                continue; // a stale entry from a priority raise
            }
            let key = job.block.0.clone();
            let index = job.block.1;
            let read = (self.read_block)(key.path.clone(), index * BLOCK_SIZE, BLOCK_SIZE);
            let this = self.clone();
            self.reads_issued.fetch_add(1, Ordering::Relaxed);
            state.running += 1;
            let task = tokio::spawn(async move {
                let outcome = read.await;
                this.finished(id, outcome);
            });
            state.jobs.get_mut(&id).expect("still here").task = Some(task.abort_handle());
        }
    }

    fn finished(self: &Arc<Self>, id: u64, outcome: Result<Bytes>) {
        let mut state = self.state.lock().unwrap();
        let Some(job) = state.jobs.remove(&id) else { return };
        state.by_block.remove(&job.block);
        state.running -= 1;
        if let Ok(data) = &outcome {
            self.cache.lock().unwrap().put(job.block.clone(), data.clone());
        }
        job.sender.send_replace(Some(outcome));
        self.pump(&mut state);
    }
}
