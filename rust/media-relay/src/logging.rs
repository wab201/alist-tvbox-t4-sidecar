use std::{
    io::{self, Write},
    sync::{
        Arc,
        atomic::Ordering,
        mpsc::{self, Receiver, SyncSender, TrySendError},
    },
    thread,
    time::{Duration, Instant},
};

use serde_json::Value;

use crate::Counters;

pub const QUEUE_CAPACITY: usize = 1024;

struct Entry {
    event: Value,
    build_ns: Option<u64>,
}

#[derive(Clone)]
pub struct AsyncLog {
    sender: SyncSender<Entry>,
    counters: Arc<Counters>,
}

pub struct Drain(Receiver<()>);

impl Drain {
    // Never let a stuck output device make process shutdown unbounded.
    pub fn wait(self, timeout: Duration) -> bool {
        self.0.recv_timeout(timeout).is_ok()
    }
}

pub fn write_event(event: &Value, build_ns: Option<u64>, counters: &Counters) {
    write_with(build_ns, counters, || {
        let mut writer = io::stdout().lock();
        writeln!(writer, "{event}").and_then(|_| writer.flush())
    });
}

fn write_with(build_ns: Option<u64>, counters: &Counters, write: impl FnOnce() -> io::Result<()>) {
    let started = build_ns.map(|_| Instant::now());
    let success = write().is_ok();
    if let Some(started) = started {
        let ns = started.elapsed().as_nanos() as u64;
        counters
            .log_encode_ns
            .fetch_add(build_ns.unwrap_or(0), Ordering::Relaxed);
        counters.log_write_ns.fetch_add(ns, Ordering::Relaxed);
        counters.log_max_write_ns.fetch_max(ns, Ordering::Relaxed);
        counters.log_events.fetch_add(1, Ordering::Relaxed);
    }
    if success {
        counters.log_written.fetch_add(1, Ordering::Relaxed);
    } else {
        counters.log_write_errors.fetch_add(1, Ordering::Relaxed);
    }
}

impl AsyncLog {
    pub fn start(counters: Arc<Counters>) -> io::Result<(Self, Drain)> {
        let (sender, receiver) = mpsc::sync_channel::<Entry>(QUEUE_CAPACITY);
        let (done, finished) = mpsc::sync_channel(1);
        let output_counters = Arc::clone(&counters);
        thread::Builder::new()
            .name("relay-log".into())
            .spawn(move || {
                while let Ok(entry) = receiver.recv() {
                    write_event(&entry.event, entry.build_ns, &output_counters);
                    output_counters.log_pending.fetch_sub(1, Ordering::Relaxed);
                }
                let _ = done.send(());
            })?;
        Ok((Self { sender, counters }, Drain(finished)))
    }

    pub fn submit(&self, event: Value, build_ns: Option<u64>, reason: &str) {
        self.counters.log_pending.fetch_add(1, Ordering::Relaxed);
        match self.sender.try_send(Entry { event, build_ns }) {
            Ok(()) => {}
            Err(error) => {
                self.counters.log_pending.fetch_sub(1, Ordering::Relaxed);
                let counter = match reason {
                    "complete" => &self.counters.log_dropped_complete,
                    "upstream_error" => &self.counters.log_dropped_error,
                    "progress" => &self.counters.log_dropped_observation,
                    _ => &self.counters.log_dropped_cancelled,
                };
                counter.fetch_add(1, Ordering::Relaxed);
                if matches!(error, TrySendError::Disconnected(_)) {
                    self.counters
                        .log_disconnected
                        .fetch_add(1, Ordering::Relaxed);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn full_queue_never_blocks_and_preserves_reason_counters() {
        let counters = Arc::new(Counters::default());
        let (sender, receiver) = mpsc::sync_channel(2);
        let logger = AsyncLog {
            sender,
            counters: Arc::clone(&counters),
        };
        logger.submit(json!({"request": 1}), None, "complete");
        logger.submit(json!({"request": 2}), None, "complete");
        for reason in [
            "complete",
            "upstream_error",
            "client_disconnect",
            "progress",
        ] {
            logger.submit(json!({"request": 3}), None, reason);
        }
        assert_eq!(counters.log_pending.load(Ordering::Relaxed), 2);
        assert_eq!(counters.log_dropped_complete.load(Ordering::Relaxed), 1);
        assert_eq!(counters.log_dropped_error.load(Ordering::Relaxed), 1);
        assert_eq!(counters.log_dropped_cancelled.load(Ordering::Relaxed), 1);
        assert_eq!(counters.log_dropped_observation.load(Ordering::Relaxed), 1);
        assert_eq!(receiver.try_iter().count(), 2);
        drop(receiver);
        logger.submit(json!({}), None, "upstream_error");
        assert_eq!(counters.log_disconnected.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn writer_errors_are_counted_without_panicking() {
        struct Broken;
        impl Write for Broken {
            fn write(&mut self, _: &[u8]) -> io::Result<usize> {
                Err(io::ErrorKind::BrokenPipe.into())
            }
            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }
        let counters = Counters::default();
        write_with(Some(1), &counters, || Broken.write_all(b"event\n"));
        assert_eq!(counters.log_write_errors.load(Ordering::Relaxed), 1);
        assert_eq!(counters.log_events.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn worker_drains_on_last_sender_drop() {
        let counters = Arc::new(Counters::default());
        let (logger, drain) = AsyncLog::start(Arc::clone(&counters)).unwrap();
        logger.submit(json!({"event": "logger.test"}), None, "complete");
        drop(logger);
        assert!(drain.wait(Duration::from_secs(1)));
        assert_eq!(counters.log_pending.load(Ordering::Relaxed), 0);
        assert_eq!(counters.log_written.load(Ordering::Relaxed), 1);
    }
}
