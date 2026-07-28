use std::ffi::OsStr;

use clap::Parser;
use codex_exec_server::ExecServerRuntimePaths;
use codex_http_client::HttpClientFactory;
use codex_http_client::OutboundProxyPolicy;

#[derive(Debug, Parser)]
#[command(version)]
struct Args {
    /// WebSocket endpoint exposed to the Codex runtime.
    #[arg(long, default_value = "ws://0.0.0.0:8081")]
    listen: String,
}

fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let mut raw_args = std::env::args_os();
    let _ = raw_args.next();
    let argv1 = raw_args.next();
    #[cfg(unix)]
    if argv1.as_deref() == Some(OsStr::new(codex_exec_server::CODEX_ARG0_EXEC_HELPER_ARG1)) {
        codex_exec_server::run_arg0_exec_helper_main();
    }
    if argv1.as_deref() == Some(OsStr::new(codex_exec_server::CODEX_FS_HELPER_ARG1)) {
        codex_exec_server::run_fs_helper_main();
    }

    let args = Args::parse();
    let runtime_paths = ExecServerRuntimePaths::new(std::env::current_exe()?, None)?;
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?
        .block_on(codex_exec_server::run_main(
            &args.listen,
            runtime_paths,
            HttpClientFactory::new(OutboundProxyPolicy::ReqwestDefault),
        ))
}
