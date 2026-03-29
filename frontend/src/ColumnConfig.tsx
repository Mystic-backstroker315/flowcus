import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { QueryColumn } from './api';
import { selectVisibleColumns } from './formatters';

interface ColumnConfigProps {
  columns: QueryColumn[];
  visibleColumns: string[];
  onChange: (columns: string[]) => void;
}

export function ColumnConfig({ columns, visibleColumns, onChange }: ColumnConfigProps) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState('');
  const popoverRef = useRef<HTMLDivElement>(null);

  // Close on outside click or Escape
  useEffect(() => {
    if (!open) return;
    const handleClick = (e: MouseEvent) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', handleClick);
    document.addEventListener('keydown', handleKey);
    return () => {
      document.removeEventListener('mousedown', handleClick);
      document.removeEventListener('keydown', handleKey);
    };
  }, [open]);

  const visibleSet = useMemo(() => new Set(visibleColumns), [visibleColumns]);

  const filteredColumns = useMemo(() => {
    if (!search) return columns;
    const q = search.toLowerCase();
    return columns.filter((c) => c.name.toLowerCase().includes(q));
  }, [columns, search]);

  const toggleColumn = useCallback(
    (name: string) => {
      if (visibleSet.has(name)) {
        onChange(visibleColumns.filter((c) => c !== name));
      } else {
        onChange([...visibleColumns, name]);
      }
    },
    [visibleColumns, visibleSet, onChange],
  );

  const selectAll = useCallback(() => {
    onChange(columns.map((c) => c.name));
  }, [columns, onChange]);

  const resetToDefault = useCallback(() => {
    const defaultIndices = selectVisibleColumns(columns);
    onChange(defaultIndices.map((i) => columns[i].name));
  }, [columns, onChange]);

  return (
    <div className="column-config" ref={popoverRef}>
      <button
        className={`column-config-trigger ${open ? 'active' : ''}`}
        onClick={() => setOpen(!open)}
        title="Configure visible columns"
      >{'\u2699'}</button>

      {open && (
        <div className="column-config-popover">
          <input
            type="text"
            className="column-config-search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search columns..."
            autoFocus
          />
          <div className="column-config-list">
            {filteredColumns.map((col) => (
              <label key={col.name} className="column-config-item">
                <input
                  type="checkbox"
                  checked={visibleSet.has(col.name)}
                  onChange={() => toggleColumn(col.name)}
                />
                <span className="column-config-name">{col.name}</span>
                <span className="column-config-type">{col.type}</span>
              </label>
            ))}
            {filteredColumns.length === 0 && (
              <div className="column-config-empty">No matching columns</div>
            )}
          </div>
          <div className="column-config-actions">
            <button onClick={selectAll}>Select all</button>
            <button onClick={resetToDefault}>Reset to default</button>
          </div>
        </div>
      )}
    </div>
  );
}
