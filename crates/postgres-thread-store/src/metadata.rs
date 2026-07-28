use chrono::DateTime;
use chrono::Utc;
use codex_git_utils::GitSha;
use codex_protocol::models::PermissionProfile;
use codex_protocol::protocol::AskForApproval;
use codex_protocol::protocol::GitInfo;
use codex_thread_store::CreateThreadParams;
use codex_thread_store::StoredThread;
use codex_thread_store::StoredThreadHistory;
use codex_thread_store::ThreadMetadataPatch;

pub(crate) struct ThreadRecord {
    pub create_params: CreateThreadParams,
    pub metadata_patch: ThreadMetadataPatch,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub recency_at: DateTime<Utc>,
    pub archived_at: Option<DateTime<Utc>>,
}

pub(crate) fn stored_thread(
    record: ThreadRecord,
    history: Option<StoredThreadHistory>,
) -> StoredThread {
    let created = record.create_params;
    let metadata = record.metadata_patch;
    let name = metadata.name.clone().flatten();
    let section = metadata
        .section
        .clone()
        .flatten()
        .map(|id| codex_state::ThreadSection {
            name: if id == codex_state::PINNED_THREAD_SECTION_ID {
                codex_state::PINNED_THREAD_SECTION_NAME.to_string()
            } else {
                id.clone()
            },
            id,
        });
    let git_info = git_info(&metadata);

    StoredThread {
        thread_id: created.thread_id,
        extra_config: created.extra_config,
        rollout_path: None,
        forked_from_id: created.forked_from_id,
        parent_thread_id: created.parent_thread_id,
        preview: metadata.preview.unwrap_or_default(),
        name,
        model_provider: metadata
            .model_provider
            .unwrap_or(created.metadata.model_provider),
        model: metadata.model,
        reasoning_effort: metadata.reasoning_effort.flatten(),
        created_at: metadata.created_at.unwrap_or(record.created_at),
        updated_at: metadata.updated_at.unwrap_or(record.updated_at),
        recency_at: metadata.advance_recency_at.unwrap_or(record.recency_at),
        archived_at: record.archived_at,
        section,
        cwd: metadata.cwd.or(created.metadata.cwd).unwrap_or_default(),
        cli_version: metadata
            .cli_version
            .unwrap_or_else(|| env!("CARGO_PKG_VERSION").to_string()),
        source: metadata.source.unwrap_or(created.source.clone()),
        history_mode: created.history_mode,
        thread_source: metadata.thread_source.flatten().or(created.thread_source),
        agent_nickname: metadata
            .agent_nickname
            .flatten()
            .or_else(|| created.source.get_nickname()),
        agent_role: metadata
            .agent_role
            .flatten()
            .or_else(|| created.source.get_agent_role()),
        agent_path: metadata
            .agent_path
            .flatten()
            .or_else(|| created.source.get_agent_path().map(Into::into)),
        git_info,
        approval_mode: metadata.approval_mode.unwrap_or(AskForApproval::Never),
        permission_profile: metadata
            .permission_profile
            .unwrap_or_else(PermissionProfile::read_only),
        token_usage: metadata.token_usage,
        first_user_message: metadata.first_user_message,
        history,
    }
}

fn git_info(metadata: &ThreadMetadataPatch) -> Option<GitInfo> {
    let patch = metadata.git_info.as_ref()?;
    let commit_hash = patch.sha.clone().flatten().as_deref().map(GitSha::new);
    let branch = patch.branch.clone().flatten();
    let repository_url = patch.origin_url.clone().flatten();
    if commit_hash.is_none() && branch.is_none() && repository_url.is_none() {
        return None;
    }
    Some(GitInfo {
        commit_hash,
        branch,
        repository_url,
    })
}
