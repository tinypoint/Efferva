use std::sync::Arc;

use efferva_postgres_thread_store::PostgresThreadStore;
use clap::Parser;
use codex_app_server::AppServerDependencies;
use codex_app_server::AppServerRuntimeOptions;
use codex_app_server::AppServerTransport;
use codex_app_server::AppServerWebsocketAuthSettings;
use codex_app_server::run_main_with_transport_options_and_dependencies;
use codex_arg0::Arg0DispatchPaths;
use codex_arg0::arg0_dispatch_or_else;
use codex_config::LoaderOverrides;
use codex_protocol::protocol::SessionSource;
use codex_thread_store::ThreadStore;
use codex_utils_cli::CliConfigOverrides;

#[derive(Debug, Parser)]
#[command(version)]
struct RuntimeArgs {
    #[command(flatten)]
    config_overrides: CliConfigOverrides,

    /// PostgreSQL connection URL. EFFERVA_DATABASE_URL takes precedence.
    #[arg(long, env = "DATABASE_URL")]
    database_url: Option<String>,

    /// Transport endpoint. `stdio://` is used by the Python control plane.
    #[arg(
        long = "listen",
        value_name = "URL",
        default_value = AppServerTransport::DEFAULT_LISTEN_URL
    )]
    listen: AppServerTransport,

    /// Fail if config.toml contains unknown fields.
    #[arg(long, default_value_t = false)]
    strict_config: bool,
}

fn main() -> anyhow::Result<()> {
    arg0_dispatch_or_else(|arg0_paths: Arg0DispatchPaths| async move {
        let args = RuntimeArgs::parse();
        let database_url = std::env::var("EFFERVA_DATABASE_URL")
            .ok()
            .or(args.database_url)
            .ok_or_else(|| {
                anyhow::anyhow!(
                    "EFFERVA_DATABASE_URL or DATABASE_URL is required for the Codex runtime"
                )
            })?;
        let thread_store: Arc<dyn ThreadStore> =
            PostgresThreadStore::connect(&database_url).await?;

        run_main_with_transport_options_and_dependencies(
            arg0_paths,
            args.config_overrides,
            LoaderOverrides::default(),
            args.strict_config,
            false,
            args.listen,
            SessionSource::from_startup_arg("app-server")
                .map_err(|error| anyhow::anyhow!(error))?,
            AppServerWebsocketAuthSettings::default(),
            AppServerRuntimeOptions::default(),
            AppServerDependencies {
                thread_store: Some(thread_store),
            },
        )
        .await?;
        Ok(())
    })
}
