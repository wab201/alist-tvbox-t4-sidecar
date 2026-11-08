use std::sync::{
    Arc,
    atomic::{AtomicU64, Ordering},
};

pub(super) const STARTUP_QUANTUM: usize = 64 * 1024;

#[derive(Default)]
pub(super) struct StartupFairness {
    waiting: AtomicU64,
    yields: AtomicU64,
}

impl StartupFairness {
    pub fn enter(self: &Arc<Self>) -> Starting {
        self.waiting.fetch_add(1, Ordering::Relaxed);
        Starting(Arc::clone(self))
    }

    pub fn waiting(&self) -> u64 {
        self.waiting.load(Ordering::Relaxed)
    }

    pub fn yields(&self) -> u64 {
        self.yields.load(Ordering::Relaxed)
    }

    pub fn has_other_startup(&self, own_startup: bool) -> bool {
        self.waiting() > u64::from(own_startup)
    }

    pub fn should_yield(&self, turn_bytes: &mut usize) -> bool {
        if self.waiting() == 0 {
            *turn_bytes = 0;
            false
        } else if *turn_bytes >= STARTUP_QUANTUM {
            *turn_bytes = 0;
            self.yields.fetch_add(1, Ordering::Relaxed);
            true
        } else {
            false
        }
    }
}

pub(super) struct Starting(Arc<StartupFairness>);

impl Drop for Starting {
    fn drop(&mut self) {
        self.0.waiting.fetch_sub(1, Ordering::Relaxed);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn startup_leases_are_released_on_completion_or_cancel() {
        let policy = Arc::new(StartupFairness::default());
        let first = policy.enter();
        assert!(!policy.has_other_startup(true));
        let second = policy.enter();
        assert!(policy.has_other_startup(true));
        drop(first);
        assert_eq!(policy.waiting(), 1);
        drop(second);
        assert_eq!(policy.waiting(), 0);
    }

    #[test]
    fn steady_streams_do_not_yield_or_wait_for_a_startup_barrier() {
        let policy = Arc::new(StartupFairness::default());
        let mut bytes = STARTUP_QUANTUM * 2;
        assert!(!policy.should_yield(&mut bytes));
        let stalled = policy.enter();
        bytes = STARTUP_QUANTUM - 1;
        assert!(!policy.should_yield(&mut bytes));
        bytes += 1;
        assert!(policy.should_yield(&mut bytes));
        assert_eq!(bytes, 0);
        assert_eq!(policy.yields(), 1);
        drop(stalled);
        bytes = STARTUP_QUANTUM;
        assert!(!policy.should_yield(&mut bytes));
    }
}
