import { useCallback, useEffect, useRef, useState } from 'react';
import { ResultsTable } from './ResultsTable';
import { FlowSidebar } from './FlowSidebar';
import { TimeRangePicker } from './TimeRangePicker';
import { SearchBar } from './SearchBar';
import { ExplainGantt, type PlanStep } from './ExplainGantt';
import { TimeHistogram } from './TimeHistogram';
import {
  executeStructuredQuery,
  fetchInfo,
  fetchInterfaces,
  fetchQuerySchema,
  type Pagination,
  type QueryError,
  type QueryStats,
  type QueryColumn,
  type ServerInfo,
  type StructuredTimeRange,
  type StructuredFilter,
  type StructuredQueryRequest,
  type SchemaResponse,
} from './api';
import { setInterfaceNames, getTimezone, setTimezone, getAvailableTimezones } from './formatters';
import { formatCache } from './formatCache';

function formatMicros(us: number): string {
  if (us < 1000) return `${us}\u00B5s`;
  if (us < 1_000_000) return `${(us / 1000).toFixed(1)}ms`;
  return `${(us / 1_000_000).toFixed(2)}s`;
}

function humanBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export function App() {
  const [info, setInfo] = useState<ServerInfo | null>(null);
  const [columns, setColumns] = useState<QueryColumn[]>([]);
  const [allRows, setAllRows] = useState<unknown[][]>([]);
  const [stats, setStats] = useState<QueryStats | null>(null);
  const [explain, setExplain] = useState<unknown[] | null>(null);
  const [pagination, setPagination] = useState<Pagination | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [queryError, setQueryError] = useState<QueryError | null>(null);
  const [selectedRow, setSelectedRow] = useState<number | null>(null);
  const [showStats, setShowStats] = useState(false);
  const [queryGen, setQueryGen] = useState(0);

  // Query state
  const [timeRange, setTimeRange] = useState<StructuredTimeRange>({ type: 'relative', duration: '5m' });
  const [filters, setFilters] = useState<StructuredFilter[]>([]);
  const [filterLogic, setFilterLogic] = useState<'and' | 'or'>('and');
  const [schema, setSchema] = useState<SchemaResponse | null>(null);

  // Column visibility
  const [visibleColumns, setVisibleColumns] = useState<string[] | null>(() => {
    try {
      const saved = localStorage.getItem('flowcus:columns');
      return saved ? JSON.parse(saved) : null;
    } catch { return null; }
  });

  // Timezone
  const [timezone, setTz] = useState(getTimezone);
  const handleTimezoneChange = useCallback((e: React.ChangeEvent<HTMLSelectElement>) => {
    const tz = e.target.value;
    setTz(tz);
    setTimezone(tz);
    formatCache.clear();
  }, []);

  // Persist visible columns
  useEffect(() => {
    if (visibleColumns) {
      localStorage.setItem('flowcus:columns', JSON.stringify(visibleColumns));
    }
  }, [visibleColumns]);

  useEffect(() => {
    fetchInfo().then(setInfo).catch(() => {});
    fetchInterfaces().then(setInterfaceNames).catch(() => {});
    fetchQuerySchema().then(setSchema).catch(() => {});
  }, []);

  // Pagination & cancellation refs
  const lastStructuredReq = useRef<StructuredQueryRequest | null>(null);
  const initialQueryFired = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const histogramChanged = useRef(false);

  const handleExecute = useCallback(async () => {
    // Cancel any in-flight query
    if (abortRef.current) abortRef.current.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    // Extract limit from filters if present — pass as aggregate stage
    const limitFilter = filters.find((f) => f.field === 'limit');
    const limitN = limitFilter ? Math.max(1, Math.min(Number(limitFilter.value) || 100, 10000)) : 0;
    const realFilters = filters.filter((f) => f.field !== 'limit' && f.field && f.op);

    const req: StructuredQueryRequest = {
      time_range: timeRange,
      filters: realFilters,
      logic: filterLogic,
      aggregate: limitN > 0 ? { type: 'limit', n: limitN } : undefined,
      offset: 0,
      limit: limitN > 0 ? limitN : 100,
    };

    setLoading(true);
    setQueryError(null);
    setSelectedRow(null);
    setQueryGen((g) => g + 1);
    lastStructuredReq.current = req;

    try {
      const res = await executeStructuredQuery(req);
      if (controller.signal.aborted) return;
      setColumns(res.columns);
      setAllRows(res.rows);
      setStats(res.stats);
      setExplain((res as unknown as Record<string, unknown>).explain as unknown[] ?? null);
      setPagination(res.pagination);
      // Pin time range for infinite scroll — subsequent pages use these
      // absolute bounds instead of re-resolving the relative duration.
      lastStructuredReq.current = {
        ...req,
        time_start: res.time_range.start,
        time_end: res.time_range.end,
      };
    } catch (err: unknown) {
      if (controller.signal.aborted) return;
      if (err && typeof err === 'object' && 'error' in err) {
        setQueryError(err as QueryError);
      } else {
        setQueryError({ error: err instanceof Error ? err.message : 'Unknown error' });
      }
    } finally {
      if (!controller.signal.aborted) {
        setLoading(false);
      }
    }
  }, [timeRange, filters, filterLogic]);

  const handleCancel = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    setLoading(false);
  }, []);

  const handleHistogramTimeChange = useCallback((range: StructuredTimeRange) => {
    setTimeRange(range);
    histogramChanged.current = true;
  }, []);

  // Auto-execute on first visit
  useEffect(() => {
    if (!initialQueryFired.current) {
      initialQueryFired.current = true;
      handleExecute();
    }
  }, [handleExecute]);

  // Auto-execute when time range changes (from picker or histogram),
  // debounced to avoid rapid re-queries during interaction.
  const prevTimeRange = useRef(timeRange);
  useEffect(() => {
    // Skip if this is the initial render (handled above)
    if (prevTimeRange.current === timeRange) return;
    prevTimeRange.current = timeRange;

    // Histogram changes fire immediately (no debounce needed)
    if (histogramChanged.current) {
      histogramChanged.current = false;
      handleExecute();
      return;
    }

    // Picker changes are debounced
    const timer = setTimeout(() => {
      handleExecute();
    }, 400);
    return () => clearTimeout(timer);
  }, [timeRange, handleExecute]);

  const loadMore = useCallback(async () => {
    if (!pagination?.has_more || loadingMore || !lastStructuredReq.current) return;

    const nextOffset = pagination.offset + pagination.limit;
    // Safety: don't load beyond the total row count
    if (nextOffset >= pagination.total) return;

    setLoadingMore(true);
    try {
      const req = { ...lastStructuredReq.current, offset: nextOffset };
      const res = await executeStructuredQuery(req);
      if (res.rows.length === 0) {
        // No more data — stop pagination regardless of has_more
        setPagination((prev) => prev ? { ...prev, has_more: false } : prev);
        return;
      }
      setAllRows((prev) => [...prev, ...res.rows]);
      setPagination(res.pagination);
      setStats(res.stats);
    } catch {
      // silently fail on scroll-load errors
    } finally {
      setLoadingMore(false);
    }
  }, [pagination, loadingMore]);

  const handleRowSelect = useCallback((index: number) => {
    setSelectedRow(index);
  }, []);

  const handleSidebarClose = useCallback(() => {
    setSelectedRow(null);
  }, []);

  const handleSidebarNavigate = useCallback((index: number) => {
    setSelectedRow(Math.max(0, Math.min(index, allRows.length - 1)));
  }, [allRows.length]);

  const timezones = getAvailableTimezones();

  return (
    <div className="app">
      <header className="app-header">
        <h1 className="app-title">Flowcus</h1>
        {info && (
          <span className="app-version">
            v{info.version}
            {info.server.dev_mode && <span className="dev-badge">DEV</span>}
          </span>
        )}
      </header>

      <section className="query-section">
        <div className="query-bar">
          <TimeRangePicker value={timeRange} onChange={setTimeRange} />

          <select
            className="tz-select"
            value={timezone}
            onChange={handleTimezoneChange}
            title="Timezone for timestamps"
          >
            {timezones.map((tz) => (
              <option key={tz} value={tz}>{tz}</option>
            ))}
          </select>
        </div>

        <SearchBar
          filters={filters}
          onChange={setFilters}
          logic={filterLogic}
          onLogicChange={setFilterLogic}
          schema={schema}
          onExecute={handleExecute}
          onCancel={handleCancel}
          loading={loading}
        />

        {queryError && (
          <div className="query-error">
            <span className="query-error-icon">!</span>
            {queryError.error}
          </div>
        )}

        {stats && (
          <div className="query-stats-bar">
            <button
              className={`stats-toggle ${showStats ? 'active' : ''}`}
              onClick={() => setShowStats(!showStats)}
              title="Query stats & execution plan"
            >
              <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
                <path d="M8 1.5a6.5 6.5 0 100 13 6.5 6.5 0 000-13zM0 8a8 8 0 1116 0A8 8 0 010 8zm6.5-.25A.75.75 0 017.25 7h1a.75.75 0 01.75.75v2.75h.25a.75.75 0 010 1.5h-2a.75.75 0 010-1.5h.25v-2h-.25a.75.75 0 01-.75-.75zM8 6a1 1 0 100-2 1 1 0 000 2z"/>
              </svg>
            </button>
            <span className="stats-summary">
              {stats.rows_returned.toLocaleString()} rows
              {stats.total_rows > 0 && ` out of ${stats.total_rows.toLocaleString()}`}
              {' in '}{formatMicros(stats.execution_time_us)}
              {stats.cached ? ' (cached)' : ''}
            </span>
          </div>
        )}

        {showStats && stats && (
          <div className="stats-panel">
            {stats.cached && (
              <div className="stats-cache-hit">Served from cache</div>
            )}
            <div className="stats-grid">
              <div className="stats-cell">
                <span className="stat-label">Parse</span>
                <span className="stat-value">{formatMicros(stats.parse_time_us)}</span>
              </div>
              <div className="stats-cell">
                <span className="stat-label">Execute</span>
                <span className="stat-value">{formatMicros(stats.execution_time_us)}{stats.cached ? ' (cached)' : ''}</span>
              </div>
              <div className="stats-cell">
                <span className="stat-label">Scanned</span>
                <span className="stat-value">{stats.rows_scanned.toLocaleString()} rows</span>
              </div>
              <div className="stats-cell">
                <span className="stat-label">Returned</span>
                <span className="stat-value">{stats.rows_returned.toLocaleString()} rows</span>
              </div>
              <div className="stats-cell">
                <span className="stat-label">Parts</span>
                <span className="stat-value">{stats.parts_scanned} scanned, {stats.parts_skipped} skipped</span>
              </div>
              <div className="stats-cell">
                <span className="stat-label">Disk read</span>
                <span className="stat-value">{humanBytes(stats.bytes_read ?? 0)}</span>
              </div>
            </div>
            {explain && explain.length > 0 && (
              <div className="explain-section">
                <div className="explain-title">Execution plan</div>
                <ExplainGantt steps={explain as PlanStep[]} stats={stats} />
              </div>
            )}
          </div>
        )}
        <TimeHistogram
          key={queryGen}
          timeRange={timeRange}
          filters={filters}
          filterLogic={filterLogic}
          onTimeRangeChange={handleHistogramTimeChange}
          queryGen={queryGen}
        />
      </section>

      {(allRows.length > 0 || loading) && (
        <section className="results-section">
          <ResultsTable
            columns={columns}
            rows={allRows}
            pagination={pagination}
            onLoadMore={loadMore}
            loadingMore={loadingMore}
            onRowSelect={handleRowSelect}
            selectedRow={selectedRow}
            visibleColumns={visibleColumns}
            onColumnConfigChange={setVisibleColumns}
          />
        </section>
      )}

      <footer className="app-footer">
        {info && (
          <span>
            {info.name} v{info.version} &mdash; {info.server.host}:{info.server.port}
          </span>
        )}
      </footer>

      {selectedRow !== null && (
        <FlowSidebar
          columns={columns}
          rows={allRows}
          selectedIndex={selectedRow}
          onClose={handleSidebarClose}
          onNavigate={handleSidebarNavigate}
          totalRows={pagination?.total ?? allRows.length}
        />
      )}
    </div>
  );
}
