use std::fmt;

#[derive(Clone, Debug)]
pub enum RvError {
    /// The connection went away; the operation may be retried after reconnect.
    Lost(String),
    NotConnected,
    NoSuchFile(String),
    PermissionDenied(String),
    Sftp(u32, String),
    Io(String),
    Refused(String),
    HostKeyUnknown { host: String, port: u16, fingerprint: String, key_line: String },
    HostKeyChanged(String),
    Auth(String),
    Cancelled,
}

impl fmt::Display for RvError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RvError::Lost(m) => write!(f, "connection lost: {m}"),
            RvError::NotConnected => write!(f, "not connected"),
            RvError::NoSuchFile(m) => write!(f, "no such file: {m}"),
            RvError::PermissionDenied(m) => write!(f, "permission denied: {m}"),
            RvError::Sftp(code, m) => write!(f, "sftp error {code}: {m}"),
            RvError::Io(m) | RvError::Refused(m) => write!(f, "{m}"),
            RvError::HostKeyUnknown { host, port, fingerprint, .. } => {
                write!(f, "unknown host key for {host}:{port}: {fingerprint}")
            }
            RvError::HostKeyChanged(m) => write!(f, "host key changed: {m}"),
            RvError::Auth(m) => write!(f, "authentication failed: {m}"),
            RvError::Cancelled => write!(f, "cancelled"),
        }
    }
}

impl From<russh::Error> for RvError {
    fn from(error: russh::Error) -> Self {
        match error {
            russh::Error::ChannelOpenFailure(reason) => {
                RvError::Refused(format!("channel refused: {reason:?}"))
            }
            other => RvError::Lost(other.to_string()),
        }
    }
}

impl From<std::io::Error> for RvError {
    fn from(error: std::io::Error) -> Self {
        RvError::Io(error.to_string())
    }
}

pub type Result<T> = std::result::Result<T, RvError>;
