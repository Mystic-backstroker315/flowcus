import { useCallback, useEffect, useRef, useState } from 'react';
import { tokenize, tokenClass } from './tokenizer';
import { useCompletions, type Completion } from './useCompletions';
import type { QueryStats } from './api';
import { ExplainGantt, type PlanStep } from './ExplainGantt';
import { getTimezone, setTimezone, getAvailableTimezones } from './formatters';
import { formatCache } from './formatCache';

interface QueryEditorProps {
  onExecute: (query: string) => void;
  loading: boolean;
  error: { error: string; position?: number; length?: number } | null;
  stats?: QueryStats | null;
  explain?: unknown[] | null;
}

export function QueryEditor({ onExecute, loading, error, stats, explain }: QueryEditorProps) {
  const [query, setQuery] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const overlayRef = useRef<HTMLDivElement>(null);
  const completionRef = useRef<HTMLDivElement>(null);
  const { getCompletions } = useCompletions();

  const [completions, setCompletions] = useState<Completion[]>([]);
  const [completionWordStart, setCompletionWordStart] = useState(0);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [showCompletions, setShowCompletions] = useState(false);
  const [completionPos, setCompletionPos] = useState({ top: 0, left: 0 });
  const [showStats, setShowStats] = useState(false);
  const [timezone, setTz] = useState(getTimezone());

  const handleTimezoneChange = useCallback((e: React.ChangeEvent<HTMLSelectElement>) => {
    const tz = e.target.value;
    setTz(tz);
    setTimezone(tz);
    formatCache.clear();
  }, []);

  const updateCompletions = useCallback(
    (text: string, cursorPos: number) => {
      const { items, wordStart } = getCompletions(text, cursorPos);
      setCompletions(items);
      setCompletionWordStart(wordStart);
      setSelectedIndex(0);
      setShowCompletions(items.length > 0);
    },
    [getCompletions],
  );

  const applyCompletion = useCallback(
    (item: Completion) => {
      const textarea = textareaRef.current;
      if (!textarea) return;
      const before = query.slice(0, completionWordStart);
      const after = query.slice(textarea.selectionStart);
      const suffix = item.kind === 'function' ? '(' : ' ';
      const newQuery = before + item.label + suffix + after;
      const newPos = completionWordStart + item.label.length + suffix.length;
      setQuery(newQuery);
      setShowCompletions(false);
      requestAnimationFrame(() => {
        textarea.focus();
        textarea.setSelectionRange(newPos, newPos);
        updateCompletions(newQuery, newPos);
      });
    },
    [query, completionWordStart, updateCompletions],
  );

  const handleChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      const text = e.target.value;
      setQuery(text);
      updateCompletions(text, e.target.selectionStart);
    },
    [updateCompletions],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        onExecute(query);
        setShowCompletions(false);
        return;
      }

      if (!showCompletions) {
        if (e.key === 'Tab') {
          e.preventDefault();
          const textarea = textareaRef.current;
          if (textarea) updateCompletions(query, textarea.selectionStart);
        }
        return;
      }

      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSelectedIndex((i) => Math.min(i + 1, completions.length - 1));
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSelectedIndex((i) => Math.max(i - 1, 0));
      } else if (e.key === 'Tab' || e.key === 'Enter') {
        if (completions.length > 0) {
          e.preventDefault();
          applyCompletion(completions[selectedIndex]);
        }
      } else if (e.key === 'Escape') {
        setShowCompletions(false);
      }
    },
    [showCompletions, completions, selectedIndex, applyCompletion, onExecute, query, updateCompletions],
  );

  const updateCaretPosition = useCallback(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    const mirror = document.createElement('div');
    const style = window.getComputedStyle(textarea);
    mirror.style.position = 'absolute';
    mirror.style.visibility = 'hidden';
    mirror.style.whiteSpace = 'pre-wrap';
    mirror.style.wordWrap = 'break-word';
    mirror.style.font = style.font;
    mirror.style.padding = style.padding;
    mirror.style.border = style.border;
    mirror.style.width = style.width;
    mirror.style.lineHeight = style.lineHeight;
    mirror.style.letterSpacing = style.letterSpacing;

    const textBefore = query.slice(0, textarea.selectionStart);
    mirror.textContent = textBefore;
    const span = document.createElement('span');
    span.textContent = '|';
    mirror.appendChild(span);
    document.body.appendChild(mirror);

    const rect = textarea.getBoundingClientRect();
    const spanRect = span.getBoundingClientRect();
    const mirrorRect = mirror.getBoundingClientRect();

    setCompletionPos({
      top: spanRect.top - mirrorRect.top + rect.top + parseInt(style.lineHeight || '20') + 4 - textarea.scrollTop,
      left: spanRect.left - mirrorRect.left + rect.left - textarea.scrollLeft,
    });

    document.body.removeChild(mirror);
  }, [query]);

  useEffect(() => {
    if (showCompletions) updateCaretPosition();
  }, [showCompletions, updateCaretPosition]);

  const handleScroll = useCallback(() => {
    if (textareaRef.current && overlayRef.current) {
      overlayRef.current.scrollTop = textareaRef.current.scrollTop;
      overlayRef.current.scrollLeft = textareaRef.current.scrollLeft;
    }
  }, []);

  const renderHighlight = useCallback(() => {
    const tokens = tokenize(query);
    return tokens.map((token, i) => {
      const cls = tokenClass(token.type);
      const hasError =
        error?.position !== undefined &&
        error.position !== null &&
        token.start < (error.position + (error.length ?? 1)) &&
        token.end > error.position;

      const className = [cls, hasError ? 'fql-error' : ''].filter(Boolean).join(' ');

      if (className) {
        return (
          <span key={i} className={className}>
            {token.value}
          </span>
        );
      }
      return token.value;
    });
  }, [query, error]);

  const hasStats = stats !== null && stats !== undefined;
  const timezones = getAvailableTimezones();

  return (
    <div className="query-editor">
      <div className="query-editor-container">
        <div ref={overlayRef} className="query-editor-overlay" aria-hidden="true">
          <pre>{renderHighlight()}{'\n'}</pre>
        </div>
        <textarea
          ref={textareaRef}
          className="query-editor-textarea"
          value={query}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          onScroll={handleScroll}
          onClick={() => {
            if (textareaRef.current) {
              updateCompletions(query, textareaRef.current.selectionStart);
            }
          }}
          spellCheck={false}
          autoComplete="off"
          autoCorrect="off"
          autoCapitalize="off"
          placeholder="Type an FQL query... Tab for suggestions"
          rows={3}
        />
      </div>

      {error && (
        <div className="query-error">
          <span className="query-error-icon">!</span>
          {error.error}
          {error.position !== undefined && (
            <span className="query-error-pos"> (at position {error.position})</span>
          )}
        </div>
      )}

      <div className="query-editor-actions">
        <button
          className="execute-btn"
          onClick={() => onExecute(query)}
          disabled={loading || query.trim().length === 0}
        >
          {loading ? (
            <>
              <span className="spinner" />
              Running...
            </>
          ) : (
            'Execute'
          )}
        </button>
        <span className="shortcut-hint">Ctrl+Enter</span>
        <span className="shortcut-hint tab-hint">Tab for suggestions</span>

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

        {hasStats && (
          <button
            className={`stats-toggle ${showStats ? 'active' : ''}`}
            onClick={() => setShowStats(!showStats)}
            title="Query stats & execution plan"
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
              <path d="M8 1.5a6.5 6.5 0 100 13 6.5 6.5 0 000-13zM0 8a8 8 0 1116 0A8 8 0 010 8zm6.5-.25A.75.75 0 017.25 7h1a.75.75 0 01.75.75v2.75h.25a.75.75 0 010 1.5h-2a.75.75 0 010-1.5h.25v-2h-.25a.75.75 0 01-.75-.75zM8 6a1 1 0 100-2 1 1 0 000 2z"/>
            </svg>
          </button>
        )}
      </div>

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

      {showCompletions && completions.length > 0 && (
        <div
          ref={completionRef}
          className="completions-dropdown"
          style={{ top: completionPos.top, left: completionPos.left }}
        >
          {completions.map((item, i) => (
            <div
              key={item.label}
              className={`completion-item ${i === selectedIndex ? 'selected' : ''}`}
              onMouseDown={(e) => {
                e.preventDefault();
                applyCompletion(item);
              }}
              onMouseEnter={() => setSelectedIndex(i)}
            >
              <span className={`completion-kind completion-kind-${item.kind}`}>
                {kindAbbrev(item.kind)}
              </span>
              <span className="completion-label">{item.label}</span>
              {item.detail && (
                <span className="completion-detail">{item.detail}</span>
              )}
            </div>
          ))}
          <div className="completion-footer">Tab to accept</div>
        </div>
      )}
    </div>
  );
}

function humanBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function kindAbbrev(kind: string): string {
  switch (kind) {
    case 'keyword': return 'key';
    case 'operator': return 'op';
    case 'direction': return 'dir';
    case 'function': return 'fn';
    case 'field': return 'fld';
    case 'port': return 'prt';
    case 'unit': return 'dur';
    case 'value': return 'val';
    case 'hint': return '\u2026';
    default: return kind.slice(0, 3);
  }
}

function formatMicros(us: number): string {
  if (us < 1000) return `${us}\u00B5s`;
  if (us < 1_000_000) return `${(us / 1000).toFixed(1)}ms`;
  return `${(us / 1_000_000).toFixed(2)}s`;
}
