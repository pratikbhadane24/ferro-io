//! Tokio runtime construction — multi-thread, all features enabled.

use std::io;

pub fn build_runtime(worker_threads: usize) -> io::Result<tokio::runtime::Runtime> {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(worker_threads)
        .thread_name("ferro-io-worker")
        .enable_all()
        .build()
}
