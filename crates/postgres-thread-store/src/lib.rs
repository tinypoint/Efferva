mod metadata;

use std::collections::HashMap;
use std::collections::HashSet;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Duration;

use codex_protocol::ThreadId;
use codex_protocol::protocol::RolloutItem;
use codex_protocol::protocol::SessionContextWindow;
use codex_protocol::protocol::SessionMeta;
use codex_protocol::protocol::SessionMetaLine;
use codex_protocol::protocol::ThreadHistoryMode;
use codex_protocol::protocol::ThreadMemoryMode;
use codex_rollout::persisted_rollout_items;
use codex_thread_store::AppendThreadItemsParams;
use codex_thread_store::ArchiveThreadParams;
use codex_thread_store::CreateThreadParams;
use codex_thread_store::DeleteThreadParams;
use codex_thread_store::ListThreadsParams;
use codex_thread_store::LoadThreadHistoryParams;
use codex_thread_store::ReadThreadByRolloutPathParams;
use codex_thread_store::ReadThreadParams;
use codex_thread_store::ResumeThreadParams;
use codex_thread_store::SortDirection;
use codex_thread_store::StoredModelContext;
use codex_thread_store::StoredThread;
use codex_thread_store::StoredThreadHistory;
use codex_thread_store::ThreadMetadataPatch;
use codex_thread_store::ThreadPage;
use codex_thread_store::ThreadRelationFilter;
use codex_thread_store::ThreadSortKey;
use codex_thread_store::ThreadStore;
use codex_thread_store::ThreadStoreError;
use codex_thread_store::ThreadStoreFuture;
use codex_thread_store::ThreadStoreResult;
use codex_thread_store::UpdateThreadMetadataParams;
use metadata::ThreadRecord;
use sqlx::PgPool;
use sqlx::Row;
use sqlx::postgres::PgPoolOptions;
use uuid::Uuid;

const DEFAULT_LEASE_TTL: Duration = Duration::from_secs(30);
const DEFAULT_RENEW_INTERVAL: Duration = Duration::from_secs(10);

pub struct PostgresThreadStore {
    pool: PgPool,
    owner_id: Uuid,
    live_writers: Mutex<HashMap<ThreadId, i64>>,
    lease_ttl: Duration,
}

impl PostgresThreadStore {
    pub async fn connect(database_url: &str) -> ThreadStoreResult<Arc<Self>> {
        let pool = PgPoolOptions::new()
            .max_connections(20)
            .connect(database_url)
            .await
            .map_err(internal)?;
        let mut migration = pool.begin().await.map_err(internal)?;
        sqlx::query(
            "SELECT pg_advisory_xact_lock(hashtext('agentframe:codex-thread-store-migrations'))",
        )
        .execute(&mut *migration)
        .await
        .map_err(internal)?;
        sqlx::raw_sql(include_str!("schema.sql"))
            .execute(&mut *migration)
            .await
            .map_err(internal)?;
        migration.commit().await.map_err(internal)?;
        let store = Arc::new(Self {
            pool,
            owner_id: Uuid::new_v4(),
            live_writers: Mutex::new(HashMap::new()),
            lease_ttl: DEFAULT_LEASE_TTL,
        });
        Self::spawn_lease_heartbeat(&store);
        Ok(store)
    }

    pub fn owner_id(&self) -> Uuid {
        self.owner_id
    }

    fn spawn_lease_heartbeat(store: &Arc<Self>) {
        let weak_store = Arc::downgrade(store);
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(DEFAULT_RENEW_INTERVAL);
            loop {
                interval.tick().await;
                let Some(store) = weak_store.upgrade() else {
                    break;
                };
                if let Err(error) = store.renew_leases().await {
                    tracing::warn!(%error, "failed to renew PostgreSQL thread-store leases");
                }
            }
        });
    }

    async fn renew_leases(&self) -> ThreadStoreResult<()> {
        let live_writers = self
            .live_writers
            .lock()
            .map_err(|_| internal_message("live writer lock poisoned"))?
            .clone();
        for (thread_id, epoch) in live_writers {
            sqlx::query(
                r#"
                UPDATE codex_engine.threads
                SET writer_expires_at = now() + make_interval(secs => $1)
                WHERE thread_id = $2
                  AND writer_owner = $3
                  AND writer_epoch = $4
                "#,
            )
            .bind(duration_seconds(self.lease_ttl))
            .bind(thread_uuid(thread_id)?)
            .bind(self.owner_id)
            .bind(epoch)
            .execute(&self.pool)
            .await
            .map_err(internal)?;
        }
        Ok(())
    }

    async fn create(&self, params: CreateThreadParams) -> ThreadStoreResult<()> {
        reject_paginated(params.history_mode)?;
        let thread_uuid = thread_uuid(params.thread_id)?;
        let session_uuid = Uuid::parse_str(&params.session_id.to_string()).map_err(internal)?;
        let create_json = serde_json::to_value(&params).map_err(internal)?;
        let session_meta = SessionMeta {
            session_id: params.session_id,
            id: params.thread_id,
            forked_from_id: params.forked_from_id,
            parent_thread_id: params.parent_thread_id,
            cwd: params.metadata.cwd.clone().unwrap_or_default(),
            agent_nickname: params.source.get_nickname(),
            agent_role: params.source.get_agent_role(),
            agent_path: params.source.get_agent_path().map(Into::into),
            originator: params.originator.clone(),
            source: params.source.clone(),
            thread_source: params.thread_source.clone(),
            model_provider: Some(params.metadata.model_provider.clone()),
            base_instructions: Some(params.base_instructions.clone()),
            dynamic_tools: (!params.dynamic_tools.is_empty()).then(|| params.dynamic_tools.clone()),
            selected_capability_roots: params.selected_capability_roots.clone(),
            memory_mode: matches!(params.metadata.memory_mode, ThreadMemoryMode::Disabled)
                .then_some("disabled".to_string()),
            history_mode: params.history_mode,
            history_base: params.history_base,
            subagent_history_start_ordinal: params.subagent_history_start_ordinal,
            multi_agent_version: params.multi_agent_version,
            context_window: Some(SessionContextWindow::new(params.initial_window_id)),
            ..SessionMeta::default()
        };
        let initial_item = serde_json::to_value(RolloutItem::SessionMeta(SessionMetaLine {
            meta: session_meta,
            git: None,
        }))
        .map_err(internal)?;
        let mut transaction = self.pool.begin().await.map_err(internal)?;
        let inserted = sqlx::query(
            r#"
            INSERT INTO codex_engine.threads(
                thread_id,
                session_id,
                create_params,
                next_ordinal,
                writer_owner,
                writer_epoch,
                writer_expires_at
            )
            VALUES (
                $1, $2, $3, 1, $4, 1, now() + make_interval(secs => $5)
            )
            ON CONFLICT (thread_id) DO NOTHING
            "#,
        )
        .bind(thread_uuid)
        .bind(session_uuid)
        .bind(create_json)
        .bind(self.owner_id)
        .bind(duration_seconds(self.lease_ttl))
        .execute(&mut *transaction)
        .await
        .map_err(internal)?;
        if inserted.rows_affected() == 0 {
            return Err(ThreadStoreError::Conflict {
                message: format!("thread {} already exists", params.thread_id),
            });
        }
        sqlx::query(
            r#"
            INSERT INTO codex_engine.rollout_items(thread_id, ordinal, payload)
            VALUES ($1, 0, $2)
            "#,
        )
        .bind(thread_uuid)
        .bind(initial_item)
        .execute(&mut *transaction)
        .await
        .map_err(internal)?;
        transaction.commit().await.map_err(internal)?;
        self.set_live_writer(params.thread_id, 1)?;
        Ok(())
    }

    async fn resume(&self, params: ResumeThreadParams) -> ThreadStoreResult<()> {
        let thread_uuid = thread_uuid(params.thread_id)?;
        let row = sqlx::query(
            r#"
            UPDATE codex_engine.threads
            SET writer_owner = $1,
                writer_epoch = writer_epoch + 1,
                writer_expires_at = now() + make_interval(secs => $2)
            WHERE thread_id = $3
              AND ($4 OR archived_at IS NULL)
              AND (
                  writer_owner IS NULL
                  OR writer_owner = $1
                  OR writer_expires_at < now()
              )
            RETURNING writer_epoch
            "#,
        )
        .bind(self.owner_id)
        .bind(duration_seconds(self.lease_ttl))
        .bind(thread_uuid)
        .bind(params.include_archived)
        .fetch_optional(&self.pool)
        .await
        .map_err(internal)?;
        let Some(row) = row else {
            return self.missing_or_conflict(params.thread_id).await;
        };
        let epoch: i64 = row.get("writer_epoch");
        self.set_live_writer(params.thread_id, epoch)?;
        Ok(())
    }

    async fn append(&self, params: AppendThreadItemsParams) -> ThreadStoreResult<()> {
        if params.items.is_empty() {
            return Ok(());
        }
        let items = persisted_rollout_items(&params.items, ThreadHistoryMode::Legacy);
        if items.is_empty() {
            return Ok(());
        }
        let epoch = self.live_writer_epoch(params.thread_id)?;
        let thread_uuid = thread_uuid(params.thread_id)?;
        let mut transaction = self.pool.begin().await.map_err(internal)?;
        let row = sqlx::query(
            r#"
            SELECT next_ordinal
            FROM codex_engine.threads
            WHERE thread_id = $1
              AND writer_owner = $2
              AND writer_epoch = $3
              AND writer_expires_at > now()
            FOR UPDATE
            "#,
        )
        .bind(thread_uuid)
        .bind(self.owner_id)
        .bind(epoch)
        .fetch_optional(&mut *transaction)
        .await
        .map_err(internal)?;
        let Some(row) = row else {
            return Err(ThreadStoreError::Conflict {
                message: format!("thread {} writer lease is stale", params.thread_id),
            });
        };
        let mut ordinal: i64 = row.get("next_ordinal");
        for item in items {
            let payload = serde_json::to_value(item).map_err(internal)?;
            sqlx::query(
                r#"
                INSERT INTO codex_engine.rollout_items(thread_id, ordinal, payload)
                VALUES ($1, $2, $3)
                "#,
            )
            .bind(thread_uuid)
            .bind(ordinal)
            .bind(payload)
            .execute(&mut *transaction)
            .await
            .map_err(internal)?;
            ordinal += 1;
        }
        sqlx::query(
            r#"
            UPDATE codex_engine.threads
            SET next_ordinal = $1,
                updated_at = now(),
                recency_at = GREATEST(recency_at, now()),
                writer_expires_at = now() + make_interval(secs => $2)
            WHERE thread_id = $3
              AND writer_owner = $4
              AND writer_epoch = $5
            "#,
        )
        .bind(ordinal)
        .bind(duration_seconds(self.lease_ttl))
        .bind(thread_uuid)
        .bind(self.owner_id)
        .bind(epoch)
        .execute(&mut *transaction)
        .await
        .map_err(internal)?;
        transaction.commit().await.map_err(internal)?;
        Ok(())
    }

    async fn barrier(&self, thread_id: ThreadId) -> ThreadStoreResult<()> {
        let epoch = self.live_writer_epoch(thread_id)?;
        let exists: bool = sqlx::query_scalar(
            r#"
            SELECT EXISTS(
                SELECT 1
                FROM codex_engine.threads
                WHERE thread_id = $1
                  AND writer_owner = $2
                  AND writer_epoch = $3
                  AND writer_expires_at > now()
            )
            "#,
        )
        .bind(thread_uuid(thread_id)?)
        .bind(self.owner_id)
        .bind(epoch)
        .fetch_one(&self.pool)
        .await
        .map_err(internal)?;
        if exists {
            Ok(())
        } else {
            Err(ThreadStoreError::Conflict {
                message: format!("thread {thread_id} writer lease is stale"),
            })
        }
    }

    async fn release_writer(&self, thread_id: ThreadId) -> ThreadStoreResult<()> {
        let epoch = self.live_writer_epoch(thread_id)?;
        sqlx::query(
            r#"
            UPDATE codex_engine.threads
            SET writer_owner = NULL, writer_expires_at = NULL
            WHERE thread_id = $1 AND writer_owner = $2 AND writer_epoch = $3
            "#,
        )
        .bind(thread_uuid(thread_id)?)
        .bind(self.owner_id)
        .bind(epoch)
        .execute(&self.pool)
        .await
        .map_err(internal)?;
        self.live_writers
            .lock()
            .map_err(|_| internal_message("live writer lock poisoned"))?
            .remove(&thread_id);
        Ok(())
    }

    async fn load_history_inner(
        &self,
        params: LoadThreadHistoryParams,
    ) -> ThreadStoreResult<StoredThreadHistory> {
        self.ensure_readable(params.thread_id, params.include_archived)
            .await?;
        let items = sqlx::query(
            r#"
            SELECT payload
            FROM codex_engine.rollout_items
            WHERE thread_id = $1
            ORDER BY ordinal
            "#,
        )
        .bind(thread_uuid(params.thread_id)?)
        .fetch_all(&self.pool)
        .await
        .map_err(internal)?
        .into_iter()
        .map(|row| serde_json::from_value::<RolloutItem>(row.get("payload")).map_err(internal))
        .collect::<ThreadStoreResult<Vec<_>>>()?;
        Ok(StoredThreadHistory {
            thread_id: params.thread_id,
            items,
        })
    }

    async fn read(&self, params: ReadThreadParams) -> ThreadStoreResult<StoredThread> {
        let record = self
            .load_record(params.thread_id, params.include_archived)
            .await?;
        let history = if params.include_history {
            Some(
                self.load_history_inner(LoadThreadHistoryParams {
                    thread_id: params.thread_id,
                    include_archived: params.include_archived,
                })
                .await?,
            )
        } else {
            None
        };
        Ok(metadata::stored_thread(record, history))
    }

    async fn list(&self, params: ListThreadsParams) -> ThreadStoreResult<ThreadPage> {
        let rows = sqlx::query(
            r#"
            SELECT create_params, metadata_patch, created_at, updated_at, recency_at, archived_at
            FROM codex_engine.threads
            WHERE ($1 AND archived_at IS NOT NULL) OR (NOT $1 AND archived_at IS NULL)
            "#,
        )
        .bind(params.archived)
        .fetch_all(&self.pool)
        .await
        .map_err(internal)?;
        let mut items = rows
            .into_iter()
            .map(record_from_row)
            .collect::<ThreadStoreResult<Vec<_>>>()?
            .into_iter()
            .map(|record| metadata::stored_thread(record, None))
            .collect::<Vec<_>>();

        let all_items = items.clone();
        items.retain(|thread| matches_filters(thread, &params, &all_items));
        items.sort_by(|left, right| {
            let order = match params.sort_key {
                ThreadSortKey::CreatedAt => left.created_at.cmp(&right.created_at),
                ThreadSortKey::UpdatedAt => left.updated_at.cmp(&right.updated_at),
                ThreadSortKey::RecencyAt => left.recency_at.cmp(&right.recency_at),
            };
            match params.sort_direction {
                SortDirection::Asc => order,
                SortDirection::Desc => order.reverse(),
            }
        });
        let offset = parse_cursor(params.cursor.as_deref())?;
        let end = offset.saturating_add(params.page_size).min(items.len());
        let page_items = if offset >= items.len() {
            Vec::new()
        } else {
            items[offset..end].to_vec()
        };
        Ok(ThreadPage {
            items: page_items,
            next_cursor: (end < items.len()).then(|| end.to_string()),
        })
    }

    async fn update_metadata(
        &self,
        params: UpdateThreadMetadataParams,
    ) -> ThreadStoreResult<StoredThread> {
        let mut transaction = self.pool.begin().await.map_err(internal)?;
        let row = sqlx::query(
            r#"
            SELECT metadata_patch
            FROM codex_engine.threads
            WHERE thread_id = $1 AND ($2 OR archived_at IS NULL)
            FOR UPDATE
            "#,
        )
        .bind(thread_uuid(params.thread_id)?)
        .bind(params.include_archived)
        .fetch_optional(&mut *transaction)
        .await
        .map_err(internal)?;
        let Some(row) = row else {
            return Err(ThreadStoreError::ThreadNotFound {
                thread_id: params.thread_id,
            });
        };
        let mut metadata_patch: ThreadMetadataPatch =
            serde_json::from_value(row.get("metadata_patch")).map_err(internal)?;
        let updated_at = params.patch.updated_at;
        let advance_recency_at = params.patch.advance_recency_at;
        metadata_patch.merge(params.patch);
        sqlx::query(
            r#"
            UPDATE codex_engine.threads
            SET metadata_patch = $1,
                updated_at = COALESCE($2, updated_at),
                recency_at = CASE
                    WHEN $3::timestamptz IS NULL THEN recency_at
                    ELSE GREATEST(recency_at, $3)
                END
            WHERE thread_id = $4
            "#,
        )
        .bind(serde_json::to_value(metadata_patch).map_err(internal)?)
        .bind(updated_at)
        .bind(advance_recency_at)
        .bind(thread_uuid(params.thread_id)?)
        .execute(&mut *transaction)
        .await
        .map_err(internal)?;
        transaction.commit().await.map_err(internal)?;
        self.read(ReadThreadParams {
            thread_id: params.thread_id,
            include_archived: params.include_archived,
            include_history: false,
        })
        .await
    }

    async fn archive(&self, thread_id: ThreadId) -> ThreadStoreResult<()> {
        let result = sqlx::query(
            r#"
            UPDATE codex_engine.threads
            SET archived_at = now(), updated_at = now()
            WHERE thread_id = $1
              AND archived_at IS NULL
              AND (writer_owner IS NULL OR writer_expires_at < now())
            "#,
        )
        .bind(thread_uuid(thread_id)?)
        .execute(&self.pool)
        .await
        .map_err(internal)?;
        if result.rows_affected() == 1 {
            Ok(())
        } else {
            self.missing_or_conflict(thread_id).await
        }
    }

    async fn unarchive(&self, thread_id: ThreadId) -> ThreadStoreResult<StoredThread> {
        let result = sqlx::query(
            r#"
            UPDATE codex_engine.threads
            SET archived_at = NULL, updated_at = now()
            WHERE thread_id = $1
            "#,
        )
        .bind(thread_uuid(thread_id)?)
        .execute(&self.pool)
        .await
        .map_err(internal)?;
        if result.rows_affected() == 0 {
            return Err(ThreadStoreError::ThreadNotFound { thread_id });
        }
        self.read(ReadThreadParams {
            thread_id,
            include_archived: true,
            include_history: false,
        })
        .await
    }

    async fn delete(&self, thread_id: ThreadId) -> ThreadStoreResult<()> {
        let result = sqlx::query(
            r#"
            DELETE FROM codex_engine.threads
            WHERE thread_id = $1
              AND (writer_owner IS NULL OR writer_expires_at < now())
            "#,
        )
        .bind(thread_uuid(thread_id)?)
        .execute(&self.pool)
        .await
        .map_err(internal)?;
        if result.rows_affected() == 1 {
            Ok(())
        } else {
            self.missing_or_conflict(thread_id).await
        }
    }

    async fn load_record(
        &self,
        thread_id: ThreadId,
        include_archived: bool,
    ) -> ThreadStoreResult<ThreadRecord> {
        let row = sqlx::query(
            r#"
            SELECT create_params, metadata_patch, created_at, updated_at, recency_at, archived_at
            FROM codex_engine.threads
            WHERE thread_id = $1 AND ($2 OR archived_at IS NULL)
            "#,
        )
        .bind(thread_uuid(thread_id)?)
        .bind(include_archived)
        .fetch_optional(&self.pool)
        .await
        .map_err(internal)?;
        row.map(record_from_row)
            .transpose()?
            .ok_or(ThreadStoreError::ThreadNotFound { thread_id })
    }

    async fn ensure_readable(
        &self,
        thread_id: ThreadId,
        include_archived: bool,
    ) -> ThreadStoreResult<()> {
        let exists: bool = sqlx::query_scalar(
            r#"
            SELECT EXISTS(
                SELECT 1 FROM codex_engine.threads
                WHERE thread_id = $1 AND ($2 OR archived_at IS NULL)
            )
            "#,
        )
        .bind(thread_uuid(thread_id)?)
        .bind(include_archived)
        .fetch_one(&self.pool)
        .await
        .map_err(internal)?;
        if exists {
            Ok(())
        } else {
            Err(ThreadStoreError::ThreadNotFound { thread_id })
        }
    }

    async fn missing_or_conflict<T>(&self, thread_id: ThreadId) -> ThreadStoreResult<T> {
        let exists: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM codex_engine.threads WHERE thread_id = $1)",
        )
        .bind(thread_uuid(thread_id)?)
        .fetch_one(&self.pool)
        .await
        .map_err(internal)?;
        if exists {
            Err(ThreadStoreError::Conflict {
                message: format!("thread {thread_id} has an active writer"),
            })
        } else {
            Err(ThreadStoreError::ThreadNotFound { thread_id })
        }
    }

    fn set_live_writer(&self, thread_id: ThreadId, epoch: i64) -> ThreadStoreResult<()> {
        self.live_writers
            .lock()
            .map_err(|_| internal_message("live writer lock poisoned"))?
            .insert(thread_id, epoch);
        Ok(())
    }

    fn live_writer_epoch(&self, thread_id: ThreadId) -> ThreadStoreResult<i64> {
        self.live_writers
            .lock()
            .map_err(|_| internal_message("live writer lock poisoned"))?
            .get(&thread_id)
            .copied()
            .ok_or_else(|| ThreadStoreError::Conflict {
                message: format!("thread {thread_id} has no writer in this runtime"),
            })
    }
}

impl ThreadStore for PostgresThreadStore {
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }

    fn create_thread(&self, params: CreateThreadParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(self.create(params))
    }

    fn resume_thread(&self, params: ResumeThreadParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(self.resume(params))
    }

    fn append_items(&self, params: AppendThreadItemsParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(self.append(params))
    }

    fn persist_thread(&self, thread_id: ThreadId) -> ThreadStoreFuture<'_, ()> {
        Box::pin(self.barrier(thread_id))
    }

    fn flush_thread(&self, thread_id: ThreadId) -> ThreadStoreFuture<'_, ()> {
        Box::pin(self.barrier(thread_id))
    }

    fn shutdown_thread(&self, thread_id: ThreadId) -> ThreadStoreFuture<'_, ()> {
        Box::pin(self.release_writer(thread_id))
    }

    fn discard_thread(&self, thread_id: ThreadId) -> ThreadStoreFuture<'_, ()> {
        Box::pin(self.release_writer(thread_id))
    }

    fn load_history(
        &self,
        params: LoadThreadHistoryParams,
    ) -> ThreadStoreFuture<'_, StoredThreadHistory> {
        Box::pin(self.load_history_inner(params))
    }

    fn load_latest_model_context(
        &self,
        params: LoadThreadHistoryParams,
    ) -> ThreadStoreFuture<'_, StoredModelContext> {
        Box::pin(async move {
            let history = self.load_history_inner(params).await?;
            Ok(StoredModelContext {
                thread_id: history.thread_id,
                items: history.items,
            })
        })
    }

    fn read_thread(&self, params: ReadThreadParams) -> ThreadStoreFuture<'_, StoredThread> {
        Box::pin(self.read(params))
    }

    fn read_thread_by_rollout_path(
        &self,
        _params: ReadThreadByRolloutPathParams,
    ) -> ThreadStoreFuture<'_, StoredThread> {
        Box::pin(async {
            Err(ThreadStoreError::Unsupported {
                operation: "read_thread_by_rollout_path",
            })
        })
    }

    fn list_threads(&self, params: ListThreadsParams) -> ThreadStoreFuture<'_, ThreadPage> {
        Box::pin(self.list(params))
    }

    fn update_thread_metadata(
        &self,
        params: UpdateThreadMetadataParams,
    ) -> ThreadStoreFuture<'_, StoredThread> {
        Box::pin(self.update_metadata(params))
    }

    fn archive_thread(&self, params: ArchiveThreadParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(self.archive(params.thread_id))
    }

    fn unarchive_thread(&self, params: ArchiveThreadParams) -> ThreadStoreFuture<'_, StoredThread> {
        Box::pin(self.unarchive(params.thread_id))
    }

    fn delete_thread(&self, params: DeleteThreadParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(self.delete(params.thread_id))
    }
}

fn record_from_row(row: sqlx::postgres::PgRow) -> ThreadStoreResult<ThreadRecord> {
    Ok(ThreadRecord {
        create_params: serde_json::from_value(row.get("create_params")).map_err(internal)?,
        metadata_patch: serde_json::from_value(row.get("metadata_patch")).map_err(internal)?,
        created_at: row.get("created_at"),
        updated_at: row.get("updated_at"),
        recency_at: row.get("recency_at"),
        archived_at: row.get("archived_at"),
    })
}

fn matches_filters(
    thread: &StoredThread,
    params: &ListThreadsParams,
    all_threads: &[StoredThread],
) -> bool {
    if !params.allowed_sources.is_empty() && !params.allowed_sources.contains(&thread.source) {
        return false;
    }
    if let Some(providers) = &params.model_providers
        && !providers.is_empty()
        && !providers.contains(&thread.model_provider)
    {
        return false;
    }
    if let Some(cwds) = &params.cwd_filters
        && !cwds.contains(&thread.cwd)
    {
        return false;
    }
    if let Some(section) = &params.section
        && thread.section.as_ref().map(|value| value.id.as_str()) != section.as_deref()
    {
        return false;
    }
    if let Some(search_term) = &params.search_term {
        let needle = search_term.to_lowercase();
        let matches = thread.preview.to_lowercase().contains(&needle)
            || thread
                .name
                .as_deref()
                .is_some_and(|name| name.to_lowercase().contains(&needle));
        if !matches {
            return false;
        }
    }
    match params.relation_filter {
        Some(ThreadRelationFilter::DirectChildrenOf(parent)) => {
            thread.parent_thread_id == Some(parent)
        }
        Some(ThreadRelationFilter::DescendantsOf(ancestor)) => {
            descendants_of(ancestor, all_threads).contains(&thread.thread_id)
        }
        None => true,
    }
}

fn descendants_of(ancestor: ThreadId, threads: &[StoredThread]) -> HashSet<ThreadId> {
    let mut descendants = HashSet::new();
    let mut frontier = HashSet::from([ancestor]);
    loop {
        let discovered = threads
            .iter()
            .filter(|thread| {
                thread
                    .parent_thread_id
                    .is_some_and(|parent| frontier.contains(&parent))
            })
            .map(|thread| thread.thread_id)
            .filter(|thread_id| descendants.insert(*thread_id))
            .collect::<HashSet<_>>();
        if discovered.is_empty() {
            return descendants;
        }
        frontier = discovered;
    }
}

fn parse_cursor(cursor: Option<&str>) -> ThreadStoreResult<usize> {
    match cursor {
        Some(cursor) => cursor
            .parse()
            .map_err(|_| ThreadStoreError::InvalidRequest {
                message: "invalid PostgreSQL thread-list cursor".to_string(),
            }),
        None => Ok(0),
    }
}

fn reject_paginated(history_mode: ThreadHistoryMode) -> ThreadStoreResult<()> {
    if matches!(history_mode, ThreadHistoryMode::Paginated) {
        Err(ThreadStoreError::Unsupported {
            operation: "paginated_threads",
        })
    } else {
        Ok(())
    }
}

fn thread_uuid(thread_id: ThreadId) -> ThreadStoreResult<Uuid> {
    Uuid::parse_str(&thread_id.to_string()).map_err(internal)
}

fn duration_seconds(duration: Duration) -> f64 {
    duration.as_secs_f64()
}

fn internal(error: impl std::fmt::Display) -> ThreadStoreError {
    internal_message(error.to_string())
}

fn internal_message(message: impl Into<String>) -> ThreadStoreError {
    ThreadStoreError::Internal {
        message: message.into(),
    }
}
