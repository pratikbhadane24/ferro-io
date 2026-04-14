//! Error propagation from Tokio tasks to Python exceptions.

use pyo3::exceptions::{PyOSError, PyRuntimeError};
use pyo3::PyErr;
use thiserror::Error;
use tokio::task::JoinError;

#[derive(Debug, Error)]
pub enum TaskError {
    #[error("task panicked: {0}")]
    Panic(String),
    #[error("task cancelled")]
    Cancelled,
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
}

impl From<JoinError> for TaskError {
    fn from(err: JoinError) -> Self {
        if err.is_panic() {
            // Try to extract the panic payload as a string.
            let payload = err.into_panic();
            let msg = if let Some(s) = payload.downcast_ref::<&'static str>() {
                (*s).to_string()
            } else if let Some(s) = payload.downcast_ref::<String>() {
                s.clone()
            } else {
                "non-string panic payload".to_string()
            };
            TaskError::Panic(msg)
        } else {
            TaskError::Cancelled
        }
    }
}

impl From<TaskError> for PyErr {
    fn from(err: TaskError) -> Self {
        match err {
            TaskError::Panic(m) => PyRuntimeError::new_err(format!("task panicked: {m}")),
            TaskError::Cancelled => PyRuntimeError::new_err("task was cancelled"),
            TaskError::Io(e) => PyOSError::new_err(e.to_string()),
        }
    }
}
