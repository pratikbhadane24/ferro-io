//! ferro_io — PyO3 bindings over a Tokio multi-thread runtime.

use std::sync::OnceLock;
use std::time::Duration;

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple};

mod errors;
mod runtime;

use errors::TaskError;

/// A handle to a process-global multi-threaded Tokio runtime.
///
/// The first call to `AsyncRuntime(worker_threads=N)` creates the runtime and
/// registers it with `pyo3_async_runtimes::tokio` so that Python `await` sites
/// (`async_sleep`, the `ferro_io` shim) share the same worker pool. Subsequent
/// calls return a handle to the already-initialized runtime — the
/// `worker_threads` argument on later calls is ignored with a best-effort
/// warning semantics (returned via `requested_worker_threads`).
#[pyclass(module = "ferro_io._ferro_io", frozen)]
pub struct AsyncRuntime {
    rt: &'static tokio::runtime::Runtime,
    worker_threads: usize,
}

static GLOBAL_RT: OnceLock<&'static tokio::runtime::Runtime> = OnceLock::new();

fn get_or_init_global(worker_threads: usize) -> PyResult<&'static tokio::runtime::Runtime> {
    if let Some(rt) = GLOBAL_RT.get() {
        return Ok(*rt);
    }
    let rt = runtime::build_runtime(worker_threads)
        .map_err(|e| PyRuntimeError::new_err(format!("failed to build tokio runtime: {e}")))?;
    let leaked: &'static tokio::runtime::Runtime = Box::leak(Box::new(rt));
    // init_with_runtime may fail if called twice; ignore the error.
    let _ = pyo3_async_runtimes::tokio::init_with_runtime(leaked);
    let stored = GLOBAL_RT.get_or_init(|| leaked);
    Ok(*stored)
}

#[pymethods]
impl AsyncRuntime {
    #[new]
    #[pyo3(signature = (worker_threads=None))]
    fn new(worker_threads: Option<usize>) -> PyResult<Self> {
        let n = worker_threads.unwrap_or_else(num_cpus::get).max(1);
        let rt = get_or_init_global(n)?;
        Ok(Self {
            rt,
            worker_threads: n,
        })
    }

    #[getter]
    fn worker_threads(&self) -> usize {
        self.worker_threads
    }

    fn __repr__(&self) -> String {
        format!("AsyncRuntime(worker_threads={})", self.worker_threads)
    }

    /// Parallelism proof: sleep `millis` on `count` tasks.
    /// 10 tasks × 100 ms should complete in ~100 ms.
    #[pyo3(signature = (count, millis))]
    fn sleep_many(&self, py: Python<'_>, count: u64, millis: u64) -> PyResult<Vec<u64>> {
        if count == 0 {
            return Ok(Vec::new());
        }
        let rt = self.rt;
        py.detach(move || -> Result<Vec<u64>, TaskError> {
            rt.block_on(async move {
                let mut set = tokio::task::JoinSet::new();
                for i in 0..count {
                    set.spawn(async move {
                        tokio::time::sleep(Duration::from_millis(millis)).await;
                        i
                    });
                }
                let mut out = Vec::with_capacity(count as usize);
                while let Some(res) = set.join_next().await {
                    out.push(res.map_err(TaskError::from)?);
                }
                out.sort_unstable();
                Ok(out)
            })
        })
        .map_err(Into::into)
    }

    /// Square each item concurrently through the Tokio scheduler.
    fn run_concurrent(&self, py: Python<'_>, items: Vec<u64>) -> PyResult<Vec<u64>> {
        let rt = self.rt;
        py.detach(move || -> Result<Vec<u64>, TaskError> {
            rt.block_on(async move {
                let mut set = tokio::task::JoinSet::new();
                for (idx, v) in items.into_iter().enumerate() {
                    set.spawn(async move {
                        tokio::task::yield_now().await;
                        (idx, v.wrapping_mul(v))
                    });
                }
                let mut out: Vec<(usize, u64)> = Vec::with_capacity(set.len());
                while let Some(res) = set.join_next().await {
                    out.push(res.map_err(TaskError::from)?);
                }
                out.sort_by_key(|(i, _)| *i);
                Ok(out.into_iter().map(|(_, v)| v).collect())
            })
        })
        .map_err(Into::into)
    }

    /// CPU-heavy work via `spawn_blocking` — truly parallel across worker threads.
    fn map_blocking(&self, py: Python<'_>, items: Vec<u64>, iterations: u64) -> PyResult<Vec<u64>> {
        let rt = self.rt;
        py.detach(move || -> Result<Vec<u64>, TaskError> {
            rt.block_on(async move {
                let mut set = tokio::task::JoinSet::new();
                for (idx, v) in items.into_iter().enumerate() {
                    set.spawn(async move {
                        let computed = tokio::task::spawn_blocking(move || {
                            let mut x = v;
                            for _ in 0..iterations {
                                x = x.wrapping_mul(2862933555777941757).wrapping_add(3037000493);
                            }
                            x
                        })
                        .await
                        .map_err(TaskError::from)?;
                        Ok::<_, TaskError>((idx, computed))
                    });
                }
                let mut out: Vec<(usize, u64)> = Vec::with_capacity(set.len());
                while let Some(res) = set.join_next().await {
                    out.push(res.map_err(TaskError::from)??);
                }
                out.sort_by_key(|(i, _)| *i);
                Ok(out.into_iter().map(|(_, v)| v).collect())
            })
        })
        .map_err(Into::into)
    }

    /// Return per-task outcomes without raising. Values in `fail_on` panic.
    #[pyo3(signature = (items, fail_on=None))]
    fn run_fallible<'py>(
        &self,
        py: Python<'py>,
        items: Vec<u64>,
        fail_on: Option<Vec<u64>>,
    ) -> PyResult<Bound<'py, PyList>> {
        let fail: std::collections::HashSet<u64> =
            fail_on.unwrap_or_default().into_iter().collect();
        let rt = self.rt;
        let results: Vec<Result<u64, String>> = py.detach(move || {
            rt.block_on(async move {
                let n = items.len();
                let mut handles: Vec<tokio::task::JoinHandle<u64>> = Vec::with_capacity(n);
                for v in items.into_iter() {
                    let should_fail = fail.contains(&v);
                    handles.push(tokio::spawn(async move {
                        tokio::task::yield_now().await;
                        if should_fail {
                            panic!("forced failure at value={v}");
                        }
                        v.wrapping_mul(v)
                    }));
                }
                let mut out: Vec<Result<u64, String>> = Vec::with_capacity(n);
                for h in handles {
                    match h.await {
                        Ok(v) => out.push(Ok(v)),
                        Err(e) if e.is_panic() => {
                            let payload = e.into_panic();
                            let msg = payload
                                .downcast_ref::<&'static str>()
                                .map(|s| (*s).to_string())
                                .or_else(|| payload.downcast_ref::<String>().cloned())
                                .unwrap_or_else(|| "panic".to_string());
                            out.push(Err(format!("task panicked: {msg}")));
                        }
                        Err(_) => out.push(Err("task cancelled".to_string())),
                    }
                }
                out
            })
        });

        let list = PyList::empty(py);
        for r in results {
            match r {
                Ok(v) => {
                    let ok_obj: Py<PyAny> = true.into_pyobject(py)?.to_owned().into_any().unbind();
                    let val_obj: Py<PyAny> = v.into_pyobject(py)?.into_any().unbind();
                    let tup = PyTuple::new(py, &[ok_obj, val_obj])?;
                    list.append(tup)?;
                }
                Err(msg) => {
                    let ok_obj: Py<PyAny> = false.into_pyobject(py)?.to_owned().into_any().unbind();
                    let val_obj: Py<PyAny> = msg.into_pyobject(py)?.into_any().unbind();
                    let tup = PyTuple::new(py, &[ok_obj, val_obj])?;
                    list.append(tup)?;
                }
            }
        }
        Ok(list)
    }

    /// Drive a Python coroutine to completion on the Tokio runtime.
    /// This backs `ferro_io.run()` — you pass a coroutine object, and it blocks
    /// the calling thread until the coroutine finishes.
    fn run_coroutine(&self, py: Python<'_>, coro: Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let asyncio = py.import("asyncio")?;
        let event_loop = asyncio.call_method0("new_event_loop")?;
        asyncio.call_method1("set_event_loop", (&event_loop,))?;

        let locals = pyo3_async_runtimes::TaskLocals::new(event_loop.clone()).copy_context(py)?;
        let fut = pyo3_async_runtimes::into_future_with_locals(&locals, coro)?;

        let result = pyo3_async_runtimes::tokio::run_until_complete(event_loop.clone(), async move {
            fut.await
        });

        // Clean up the loop we created.
        let _ = event_loop.call_method0("close");
        let _ = asyncio.call_method1("set_event_loop", (py.None(),));

        result
    }
}

/// `await ferro_io.async_sleep(0.05)` — a Python awaitable backed by Tokio.
#[pyfunction]
fn async_sleep(py: Python<'_>, delay: f64) -> PyResult<Bound<'_, PyAny>> {
    if !delay.is_finite() || delay < 0.0 {
        return Err(PyValueError::new_err(
            "delay must be a non-negative finite number",
        ));
    }
    // Ensure runtime exists (idempotent).
    get_or_init_global(num_cpus::get())?;
    let secs = Duration::from_secs_f64(delay);
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        tokio::time::sleep(secs).await;
        Ok(())
    })
}

#[pymodule]
fn _ferro_io(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_class::<AsyncRuntime>()?;
    m.add_function(wrap_pyfunction!(async_sleep, m)?)?;
    Ok(())
}
