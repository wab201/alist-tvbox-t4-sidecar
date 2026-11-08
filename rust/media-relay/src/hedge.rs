use super::{App, Asset, Counters, Transfer, unix_us};
use axum::{
    body::Bytes,
    http::{HeaderMap, Method},
};
use std::{
    future::Future,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

pub(super) struct HedgePolicy {
    pub delay: Option<Duration>,
    pub limit: u64,
    pub(super) eligible: AtomicU64,
    pub(super) credits: AtomicU64,
}

impl Default for HedgePolicy {
    fn default() -> Self {
        Self {
            delay: None,
            limit: 2,
            eligible: AtomicU64::new(0),
            credits: AtomicU64::new(2),
        }
    }
}

impl HedgePolicy {
    fn enter(&self) {
        let eligible = self.eligible.fetch_add(1, Ordering::Relaxed) + 1;
        if eligible.is_multiple_of(8) {
            let _ = self
                .credits
                .try_update(Ordering::Relaxed, Ordering::Relaxed, |credits| {
                    Some((credits + 1).min(2))
                });
        }
    }

    fn try_credit(&self) -> bool {
        self.credits
            .try_update(Ordering::Relaxed, Ordering::Relaxed, |credits| {
                credits.checked_sub(1)
            })
            .is_ok()
    }
}

pub(super) struct Opened {
    pub response: reqwest::Response,
    pub prefix: Option<Bytes>,
    pub headers_at: Instant,
    pub headers_us: u64,
    pub body_race: Option<BodyRace>,
}

pub(super) struct BodyRace {
    headers: HeaderMap,
    deadline: tokio::time::Instant,
}

struct ExtraRequest(Arc<Counters>);

impl ExtraRequest {
    fn try_reserve(counters: &Arc<Counters>, limit: u64) -> Option<Self> {
        let previous = counters
            .hedge_active
            .try_update(Ordering::Relaxed, Ordering::Relaxed, |active| {
                (active < limit).then_some(active + 1)
            })
            .ok()?;
        counters
            .hedge_peak
            .fetch_max(previous + 1, Ordering::Relaxed);
        Some(Self(Arc::clone(counters)))
    }
}

impl Drop for ExtraRequest {
    fn drop(&mut self) {
        self.0.hedge_active.fetch_sub(1, Ordering::Relaxed);
    }
}

fn acceptable(result: &Result<Opened, &'static str>) -> bool {
    result
        .as_ref()
        .is_ok_and(|opened| opened.response.status().is_success() && opened.prefix.is_some())
}

fn strong_etag(headers: &HeaderMap) -> bool {
    if headers.get_all("etag").iter().count() != 1 {
        return false;
    }
    let value = headers["etag"].as_bytes();
    value.len() >= 2
        && value.first() == Some(&b'"')
        && value.last() == Some(&b'"')
        && value[1..value.len() - 1]
            .iter()
            .all(|byte| *byte == 0x21 || (0x23..=0x7e).contains(byte) || *byte >= 0x80)
}

fn can_commit_headers(opened: &Opened) -> bool {
    matches!(opened.response.status().as_u16(), 200 | 206)
        && opened
            .response
            .content_length()
            .is_some_and(|length| length > 0)
        && strong_etag(opened.response.headers())
}

fn same_media_headers(expected: &HeaderMap, actual: &HeaderMap) -> bool {
    [
        "content-type",
        "content-length",
        "content-range",
        "accept-ranges",
        "etag",
        "last-modified",
        "content-encoding",
    ]
    .iter()
    .all(|name| {
        // Preserve duplicate-field semantics too, not just the first value.
        expected
            .get_all(*name)
            .iter()
            .eq(actual.get_all(*name).iter())
    })
}

impl App {
    async fn prepare_media(
        &self,
        method: Method,
        asset: &Asset,
        headers: HeaderMap,
        prefetch: bool,
    ) -> Result<Opened, &'static str> {
        let response = self.upstream(method, asset.url.clone(), headers).await?;
        let headers_at = Instant::now();
        let headers_us = unix_us();
        let opened = Opened {
            response,
            prefix: None,
            headers_at,
            headers_us,
            body_race: None,
        };
        if prefetch {
            Self::prefetch(opened).await
        } else {
            Ok(opened)
        }
    }

    async fn prefetch(mut opened: Opened) -> Result<Opened, &'static str> {
        if opened.response.status().is_success() && opened.response.content_length() != Some(0) {
            loop {
                match opened.response.chunk().await {
                    Ok(Some(bytes)) if bytes.is_empty() => continue,
                    Ok(Some(bytes)) => {
                        opened.prefix = Some(bytes);
                        break;
                    }
                    Ok(None) => break,
                    Err(_) => return Err("upstream first body failed"),
                }
            }
        }
        Ok(opened)
    }

    pub(super) async fn open_media(
        &self,
        method: Method,
        asset: &Asset,
        headers: HeaderMap,
        transfer: &mut Transfer,
    ) -> Result<Opened, &'static str> {
        let Some(delay) = self
            .hedge
            .delay
            .filter(|_| method == Method::GET && asset.hedge)
        else {
            return self.prepare_media(method, asset, headers, false).await;
        };
        self.hedge.enter();
        // If a backup cannot start now, do not hold even fast primary headers
        // behind a first-body prefetch. Recheck at the deadline for actual races.
        if self.skip_unavailable_hedge(asset, transfer) {
            return self.prepare_media(method, asset, headers, false).await;
        }
        let deadline = tokio::time::Instant::now() + delay;
        let primary = self.prepare_media(method.clone(), asset, headers.clone(), false);
        tokio::pin!(primary);
        tokio::select! {
            biased;
            result = &mut primary => {
                let mut opened = result?;
                if can_commit_headers(&opened) {
                    // A strong representation validator permits early headers.
                    // A backup may supply bytes only with an identical contract.
                    opened.body_race = Some(BodyRace { headers, deadline });
                    return Ok(opened);
                }
                return self.race_body(
                    Self::prefetch(opened), asset, headers, transfer, deadline, None,
                ).await;
            },
            _ = tokio::time::sleep_until(deadline) => {}
        }
        let primary = async { Self::prefetch(primary.await?).await };
        self.race_body(primary, asset, headers, transfer, deadline, None)
            .await
    }

    pub(super) async fn resume_body(
        &self,
        mut opened: Opened,
        asset: &Asset,
        transfer: &mut Transfer,
    ) -> Result<Opened, &'static str> {
        let race = opened.body_race.take().expect("deferred body race");
        let expected = (opened.response.status(), opened.response.headers().clone());
        self.race_body(
            Self::prefetch(opened),
            asset,
            race.headers,
            transfer,
            race.deadline,
            Some(expected),
        )
        .await
    }

    async fn race_body(
        &self,
        primary: impl Future<Output = Result<Opened, &'static str>>,
        asset: &Asset,
        headers: HeaderMap,
        transfer: &mut Transfer,
        deadline: tokio::time::Instant,
        expected: Option<(axum::http::StatusCode, HeaderMap)>,
    ) -> Result<Opened, &'static str> {
        tokio::pin!(primary);
        tokio::select! {
            biased;
            result = &mut primary => return result,
            _ = tokio::time::sleep_until(deadline) => {}
        }
        let now = self.started.elapsed().as_millis() as u64;
        if asset.hedge_cooldown_until_ms.load(Ordering::Relaxed) > now {
            self.counters.hedge_skipped.fetch_add(1, Ordering::Relaxed);
            self.counters
                .hedge_cooldown_skipped
                .fetch_add(1, Ordering::Relaxed);
            transfer.hedge_skipped = true;
            transfer.hedge_cooldown = true;
            return primary.await;
        }
        let Some(_permit) = ExtraRequest::try_reserve(&self.counters, self.hedge.limit) else {
            self.counters.hedge_skipped.fetch_add(1, Ordering::Relaxed);
            transfer.hedge_skipped = true;
            return primary.await;
        };
        if !self.hedge.try_credit() {
            self.counters.hedge_skipped.fetch_add(1, Ordering::Relaxed);
            self.counters
                .hedge_budget_skipped
                .fetch_add(1, Ordering::Relaxed);
            transfer.hedge_skipped = true;
            transfer.hedge_budget = true;
            drop(_permit);
            return primary.await;
        }
        self.counters.hedge_started.fetch_add(1, Ordering::Relaxed);
        transfer.hedged = true;
        let backup = async {
            let opened = self
                .prepare_media(Method::GET, asset, headers, false)
                .await?;
            if let Some((status, headers)) = expected.as_ref()
                && (opened.response.status() != *status
                    || !same_media_headers(headers, opened.response.headers()))
            {
                self.counters.hedge_rejected.fetch_add(1, Ordering::Relaxed);
                return Err("backup media contract changed");
            }
            Self::prefetch(opened).await
        };
        tokio::pin!(backup);
        // Both futures own their responses. Returning drops the loser, including
        // its socket/body; no detached task continues downloading after selection.
        let (winner, result) = tokio::select! {
            biased;
            original = &mut primary => {
                if acceptable(&original) { (0, original) } else {
                    let second = backup.await;
                    if acceptable(&second) { (1, second) } else { (0, original) }
                }
            }
            second = &mut backup => {
                if acceptable(&second) { (1, second) } else {
                    (0, primary.await)
                }
            }
        };
        transfer.hedge_winner = winner;
        if winner == 1 {
            self.counters.hedge_wins.fetch_add(1, Ordering::Relaxed);
        } else {
            // Correlated slowness should not keep multiplying upstream requests.
            // State is bounded by registered assets and uses a monotonic clock.
            asset.hedge_cooldown_until_ms.fetch_max(
                (self.started.elapsed().as_millis() as u64).saturating_add(1000),
                Ordering::Relaxed,
            );
        }
        result
    }

    fn skip_unavailable_hedge(&self, asset: &Asset, transfer: &mut Transfer) -> bool {
        let cooldown = asset.hedge_cooldown_until_ms.load(Ordering::Relaxed)
            > self.started.elapsed().as_millis() as u64;
        let budget = self.hedge.credits.load(Ordering::Relaxed) == 0;
        let busy = self.counters.hedge_active.load(Ordering::Relaxed) >= self.hedge.limit;
        if !cooldown && !budget && !busy {
            return false;
        }
        self.counters.hedge_skipped.fetch_add(1, Ordering::Relaxed);
        transfer.hedge_skipped = true;
        if cooldown {
            self.counters
                .hedge_cooldown_skipped
                .fetch_add(1, Ordering::Relaxed);
            transfer.hedge_cooldown = true;
        } else if budget {
            self.counters
                .hedge_budget_skipped
                .fetch_add(1, Ordering::Relaxed);
            transfer.hedge_budget = true;
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn early_headers_require_a_single_strong_validator_and_exact_media_contract() {
        for (tag, valid) in [
            ("\"revision-1\"", true),
            ("\"\"", true),
            ("W/\"revision-1\"", false),
            ("\"a\", \"b\"", false),
            ("\"has a space\"", false),
            ("revision-1", false),
        ] {
            let mut headers = HeaderMap::new();
            headers.insert("etag", tag.parse().unwrap());
            assert_eq!(strong_etag(&headers), valid);
        }
        let mut original = HeaderMap::new();
        original.insert("etag", "\"revision-1\"".parse().unwrap());
        original.insert("content-range", "bytes 0-8191/65536".parse().unwrap());
        assert!(same_media_headers(&original, &original));
        let mut changed = original.clone();
        changed.insert("content-range", "bytes 8192-16383/65536".parse().unwrap());
        assert!(!same_media_headers(&original, &changed));
        changed = original.clone();
        changed.append("etag", "\"revision-1\"".parse().unwrap());
        assert!(!strong_etag(&changed));
        assert!(!same_media_headers(&original, &changed));
    }

    #[test]
    fn extra_request_budget_is_non_queueing_and_released_on_drop() {
        let counters = Arc::new(Counters::default());
        let first = ExtraRequest::try_reserve(&counters, 1).unwrap();
        assert!(ExtraRequest::try_reserve(&counters, 1).is_none());
        assert_eq!(counters.hedge_peak.load(Ordering::Relaxed), 1);
        drop(first);
        assert_eq!(counters.hedge_active.load(Ordering::Relaxed), 0);
        assert!(ExtraRequest::try_reserve(&counters, 1).is_some());
    }

    #[test]
    fn credits_limit_amplification_and_replenish_without_accumulating_a_burst() {
        let policy = HedgePolicy::default();
        assert!(policy.try_credit());
        assert!(policy.try_credit());
        assert!(!policy.try_credit());
        for _ in 0..7 {
            policy.enter();
        }
        assert!(!policy.try_credit());
        policy.enter();
        assert!(policy.try_credit());
        assert!(!policy.try_credit());
        for _ in 0..80 {
            policy.enter();
        }
        assert!(policy.try_credit());
        assert!(policy.try_credit());
        assert!(!policy.try_credit());
    }
}
