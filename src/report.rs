//! Terminal report: findings in plain language first, evidence after.

use std::fmt::Write;

use crate::analysis::{Capture, SiteRow};

pub fn human_bytes(n: u64) -> String {
    const UNITS: [&str; 5] = ["B", "KiB", "MiB", "GiB", "TiB"];
    let mut v = n as f64;
    let mut u = 0;
    while v >= 1024.0 && u < UNITS.len() - 1 {
        v /= 1024.0;
        u += 1;
    }
    if u == 0 {
        format!("{n} B")
    } else {
        format!("{v:.1} {}", UNITS[u])
    }
}

fn short_path(file: &str, cwd: Option<&str>) -> String {
    if let Some(cwd) = cwd {
        let cwd = cwd.trim_end_matches(['/', '\\']);
        if let Some(rest) = file.strip_prefix(cwd) {
            let rest = rest.trim_start_matches(['/', '\\']);
            if !rest.is_empty() {
                return rest.to_string();
            }
        }
    }
    file.to_string()
}

fn location(r: &SiteRow, cwd: Option<&str>) -> String {
    if r.file.is_empty() {
        return String::new();
    }
    format!("{}:{}", short_path(&r.file, cwd), r.line)
}

/// "load_rows() at etl.py:7", or the bare label for sites with no file.
fn culprit(r: &SiteRow, cwd: Option<&str>) -> String {
    if r.file.is_empty() {
        r.func.clone()
    } else {
        format!("{}() at {}", r.func, location(r, cwd))
    }
}

fn pct(part: u64, whole: u64) -> f64 {
    if whole == 0 {
        0.0
    } else {
        part as f64 * 100.0 / whole as f64
    }
}

/// "heapwise · app.py · 1.20 s · sampled, one per 32.0 KiB on average · Python + native"
pub fn header(cap: &Capture, title: &str) -> String {
    let mode = if cap.sample_rate == 0 {
        "every allocation".to_string()
    } else {
        format!("sampled, one per {} on average", human_bytes(cap.sample_rate))
    };
    let coverage = if cap.native_supported {
        "Python + native"
    } else {
        "malloc/mmap from C extensions not tracked on this OS yet"
    };
    format!("heapwise · {title} · {:.2} s · {mode} · {coverage}", cap.duration_ns as f64 / 1e9)
}

/// Peak, what is still allocated at exit, and the biggest sites at the peak.
pub fn summary(cap: &Capture, title: &str, exclude: &[String], cwd: Option<&str>, top: usize) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "heapwise · {title} · {:.2} s", cap.duration_ns as f64 / 1e9);
    if cap.peak_bytes == 0 {
        let _ = writeln!(out, "No Python heap allocations were recorded.");
        return out;
    }
    let leaked: u64 = cap.live_at_end.values().map(|a| a.bytes).sum();
    let _ = writeln!(out, "Peak     {} at {:.2} s", human_bytes(cap.peak_bytes), cap.peak_t_ns as f64 / 1e9);
    let _ = writeln!(out, "At exit  {}", human_bytes(leaked));
    for (i, r) in cap.rows("peak", exclude).iter().take(top).enumerate() {
        let label = if i == 0 { "Top" } else { "" };
        let _ = writeln!(
            out,
            "{label:<8} {} ({:.0}% of peak)  {}",
            human_bytes(r.bytes),
            pct(r.bytes, cap.peak_bytes),
            culprit(r, cwd)
        );
    }
    out
}
