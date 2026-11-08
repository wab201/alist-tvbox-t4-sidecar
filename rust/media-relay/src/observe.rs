//! Opt-in startup probes. Future wake-to-repoll is not pure scheduler or socket latency.
use std::{
    future::{Future, poll_fn},
    pin::pin,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    task::{Context, Wake, Waker},
    time::Instant,
};

use futures_util::task::AtomicWaker;
use serde_json::{Value, json};

pub(super) struct Observation {
    epoch: Instant,
    epoch_us: u64,
    wake_ns: AtomicU64,
    polls: AtomicU64,
    wakes: AtomicU64,
    poll_ns: AtomicU64,
    max_poll_ns: AtomicU64,
    wake_to_poll_ns: AtomicU64,
    max_wake_to_poll_ns: AtomicU64,
    pending_ns: AtomicU64,
    pending_without_wake: AtomicU64,
    first_poll_us: AtomicU64,
    ready_us: AtomicU64,
}

impl Observation {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            epoch: Instant::now(),
            epoch_us: super::unix_us(),
            wake_ns: AtomicU64::new(0),
            polls: AtomicU64::new(0),
            wakes: AtomicU64::new(0),
            poll_ns: AtomicU64::new(0),
            max_poll_ns: AtomicU64::new(0),
            wake_to_poll_ns: AtomicU64::new(0),
            max_wake_to_poll_ns: AtomicU64::new(0),
            pending_ns: AtomicU64::new(0),
            pending_without_wake: AtomicU64::new(0),
            first_poll_us: AtomicU64::new(0),
            ready_us: AtomicU64::new(0),
        })
    }

    fn now_ns(&self) -> u64 {
        self.epoch.elapsed().as_nanos() as u64 + 1
    }

    pub fn snapshot(&self) -> Value {
        let load = |n: &AtomicU64| n.load(Ordering::Relaxed);
        json!({
            "polls": load(&self.polls), "wakes": load(&self.wakes),
            "poll_ns": load(&self.poll_ns), "max_poll_ns": load(&self.max_poll_ns),
            "wake_to_repoll_ns": load(&self.wake_to_poll_ns),
            "max_wake_to_repoll_ns": load(&self.max_wake_to_poll_ns),
            "pending_ns": load(&self.pending_ns),
            "pending_without_recorded_wake": load(&self.pending_without_wake),
            "first_poll_us": load(&self.first_poll_us), "ready_us": load(&self.ready_us),
        })
    }
}

struct ObservedWake {
    parent: AtomicWaker,
    observation: Arc<Observation>,
}

impl Wake for ObservedWake {
    fn wake(self: Arc<Self>) {
        self.wake_by_ref();
    }

    fn wake_by_ref(self: &Arc<Self>) {
        self.observation.wakes.fetch_add(1, Ordering::Relaxed);
        let _ = self.observation.wake_ns.compare_exchange(
            0,
            self.observation.now_ns(),
            Ordering::AcqRel,
            Ordering::Relaxed,
        );
        self.parent.wake();
    }
}

pub(super) async fn observed<F: Future>(
    future: F,
    observation: Option<&Arc<Observation>>,
) -> F::Output {
    let Some(stats) = observation else {
        return future.await;
    };
    let notify = Arc::new(ObservedWake {
        parent: AtomicWaker::new(),
        observation: Arc::clone(stats),
    });
    let waker = Waker::from(Arc::clone(&notify));
    let mut future = pin!(future);
    let mut pending_since = None;
    poll_fn(|cx| {
        notify.parent.register(cx.waker());
        let started = stats.now_ns();
        stats.polls.fetch_add(1, Ordering::Relaxed);
        let _ = stats.first_poll_us.compare_exchange(
            0,
            stats.epoch_us + started / 1000,
            Ordering::Relaxed,
            Ordering::Relaxed,
        );
        let woke = stats.wake_ns.swap(0, Ordering::AcqRel);
        if let Some(pending) = pending_since.take() {
            stats
                .pending_ns
                .fetch_add(started - pending, Ordering::Relaxed);
            if woke > 0 {
                // Wakes while the previous poll ran are bounded to its Pending return.
                let delay = started.saturating_sub(woke.max(pending));
                stats.wake_to_poll_ns.fetch_add(delay, Ordering::Relaxed);
                stats
                    .max_wake_to_poll_ns
                    .fetch_max(delay, Ordering::Relaxed);
            } else {
                stats.pending_without_wake.fetch_add(1, Ordering::Relaxed);
            }
        }
        let result = future.as_mut().poll(&mut Context::from_waker(&waker));
        let ended = stats.now_ns();
        stats.poll_ns.fetch_add(ended - started, Ordering::Relaxed);
        stats
            .max_poll_ns
            .fetch_max(ended - started, Ordering::Relaxed);
        if result.is_pending() {
            pending_since = Some(ended);
        } else {
            stats
                .ready_us
                .store(stats.epoch_us + ended / 1000, Ordering::Relaxed);
        }
        result
    })
    .await
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{task::Poll, time::Duration};

    #[tokio::test]
    async fn immediate_future_and_disabled_probe_preserve_output() {
        let stats = Observation::new();
        assert_eq!(observed(async { 7 }, Some(&stats)).await, 7);
        assert_eq!(stats.snapshot()["polls"], 1);
        assert_eq!(stats.snapshot()["pending_ns"], 0);
        assert_eq!(observed(async { 9 }, None).await, 9);
    }

    #[tokio::test]
    async fn timer_wakeup_is_forwarded_and_wait_is_bounded() {
        let stats = Observation::new();
        observed(tokio::time::sleep(Duration::from_millis(5)), Some(&stats)).await;
        let v = stats.snapshot();
        assert!(v["wakes"].as_u64().unwrap() > 0);
        assert!(v["pending_ns"].as_u64().unwrap() >= v["wake_to_repoll_ns"].as_u64().unwrap());
        assert!(v["ready_us"].as_u64().unwrap() >= v["first_poll_us"].as_u64().unwrap());
    }

    #[tokio::test]
    async fn self_wake_during_poll_is_not_lost() {
        let stats = Observation::new();
        let mut count = 0;
        let result = observed(
            poll_fn(|cx| {
                count += 1;
                if count == 1 {
                    cx.waker().wake_by_ref();
                    Poll::Pending
                } else {
                    Poll::Ready(count)
                }
            }),
            Some(&stats),
        )
        .await;
        assert_eq!(result, 2);
        assert_eq!(stats.snapshot()["polls"], 2);
        assert_eq!(stats.snapshot()["wakes"], 1);
    }
}
