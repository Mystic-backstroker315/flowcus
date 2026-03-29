import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { StructuredFilter, SchemaResponse, SchemaField } from './api';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface SearchBarProps {
  filters: StructuredFilter[];
  onChange: (filters: StructuredFilter[]) => void;
  logic: 'and' | 'or';
  onLogicChange: (logic: 'and' | 'or') => void;
  schema: SchemaResponse | null;
  onExecute: () => void;
  onCancel: () => void;
  loading: boolean;
}

type CompletionState = 'idle' | 'field' | 'op' | 'value';
type EditTarget = { index: number; part: 'field' | 'op' | 'value' } | null;

interface HistoryEntry {
  filters: StructuredFilter[];
  logic: 'and' | 'or';
}

interface SuggestionItem {
  key: string;
  label: string;
  detail: string;
  data: unknown;
  group?: string; // optional group header
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const OP_LABELS: Record<string, string> = {
  eq: '=', ne: '!=', gt: '>', ge: '>=', lt: '<', le: '<=',
  in: 'in', not_in: 'not in', between: 'between',
  cidr: 'cidr', wildcard: 'wildcard', ip_range: 'range',
  regex: '~', not_regex: '!~',
  contains: 'contains', not_contains: '!contains',
  starts_with: 'starts with', ends_with: 'ends with',
  port_range: 'range', named: 'is', prefix: 'prefix',
};

/** Special pseudo-field for row limit */
const LIMIT_FIELD = 'limit';

function isLimitFilter(f: StructuredFilter): boolean {
  return f.field === LIMIT_FIELD;
}

const OP_FROM_LABEL = Object.fromEntries(
  Object.entries(OP_LABELS).map(([k, v]) => [v, k]),
);

const COMMON_PROTOCOLS = ['tcp', 'udp', 'icmp', 'gre', 'sctp', 'esp', 'ah'];
const COMMON_SERVICES = [
  'http', 'https', 'dns', 'ssh', 'ftp', 'smtp', 'snmp',
  'telnet', 'ntp', 'imap', 'pop3', 'ldap', 'rdp', 'mysql',
  'postgresql', 'redis', 'syslog', 'bgp', 'netflow',
];

// Aliases ordered by network engineer workflow:
// 1. 5-tuple identity (what you always filter on)
// 2. Traffic volume (bytes/packets — the next question is always "how much")
// 3. TCP/QoS flags (next: what kind of traffic)
// 4. Routing & interfaces (where did it go)
// 5. L2 / VLAN (switching context)
// 6. ICMP
// 7. Timing / duration
// 8. Application layer
// 9. Exporter metadata
const ALIAS_ORDER: string[] = [
  // 5-tuple
  'src', 'dst', 'sport', 'dport', 'port', 'proto',
  // Counters
  'bytes', 'packets',
  // TCP / QoS
  'flags', 'tcpflags', 'tos', 'dscp',
  // Routing
  'nexthop', 'nexthop6', 'bgp_nexthop', 'src_as', 'dst_as', 'srcas', 'dstas',
  // Interfaces
  'ingress', 'egress', 'in_if', 'out_if',
  // L2
  'vlan', 'src_mac', 'dst_mac', 'srcmac', 'dstmac',
  // ICMP
  'icmp_type', 'icmp6_type',
  // Timing
  'duration', 'start', 'end',
  // Application
  'app',
  // Exporter
  'exporter', 'domain_id',
];
const ALIAS_RANK = new Map(ALIAS_ORDER.map((name, i) => [name, i]));
const ALIASES = new Set(ALIAS_ORDER);

const FIELD_PRIORITY: Record<string, number> = {
  sourceIPv4Address: 0, destinationIPv4Address: 0,
  sourceIPv6Address: 0, destinationIPv6Address: 0,
  sourceTransportPort: 0, destinationTransportPort: 0,
  protocolIdentifier: 0,
  octetDeltaCount: 1, packetDeltaCount: 1,
  flowStartMilliseconds: 1, flowEndMilliseconds: 1,
  flowStartSeconds: 1, flowEndSeconds: 1,
  flowDurationMilliseconds: 1,
  ingressInterface: 2, egressInterface: 2,
  bgpSourceAsNumber: 2, bgpDestinationAsNumber: 2,
  ipNextHopIPv4Address: 2, ipNextHopIPv6Address: 2,
  bgpNextHopIPv4Address: 2, bgpNextHopIPv6Address: 2,
  vlanId: 2, postVlanId: 2, dot1qVlanId: 2,
  ipClassOfService: 2, tcpControlBits: 2,
  sourceMacAddress: 2, destinationMacAddress: 2,
  icmpTypeCodeIPv4: 2, icmpTypeCodeIPv6: 2,
  applicationName: 2, applicationId: 2,
  mplsTopLabelStackSection: 2,
  flowcusExporterIPv4: 3, flowcusExporterPort: 3,
  flowcusExportTime: 3, flowcusObservationDomainId: 3,
  flowcusFlowDuration: 3,
};

function rankField(f: SchemaField): [number, number] {
  const aliasIdx = ALIAS_RANK.get(f.name);
  if (aliasIdx !== undefined) return [-1, aliasIdx];
  return [FIELD_PRIORITY[f.name] ?? 4, 0];
}

const HISTORY_KEY = 'flowcus:query-history';
const HISTORY_MAX = 15;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function loadHistory(): HistoryEntry[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.slice(0, HISTORY_MAX) : [];
  } catch { return []; }
}

function saveHistory(entry: HistoryEntry): void {
  const history = loadHistory();
  const key = JSON.stringify(entry);
  const filtered = history.filter((h) => JSON.stringify(h) !== key);
  filtered.unshift(entry);
  if (filtered.length > HISTORY_MAX) filtered.length = HISTORY_MAX;
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(filtered)); } catch { /* full */ }
}

function filtersToText(filters: StructuredFilter[], logic: 'and' | 'or'): string {
  if (filters.length === 0) return '';
  return filters
    .map((f) => isLimitFilter(f) ? `limit ${String(f.value)}` : `${f.field} ${OP_LABELS[f.op] ?? f.op} ${String(f.value)}`)
    .join(` ${logic} `);
}

function textToFilters(text: string): { filters: StructuredFilter[]; logic: 'and' | 'or' } | null {
  const trimmed = text.trim();
  if (!trimmed) return null;

  let logic: 'and' | 'or' = 'and';
  let parts: string[];
  // Split on " or " or " and " — detect which is used
  if (/\s+or\s+/.test(trimmed) && !/\s+and\s+/.test(trimmed)) {
    logic = 'or';
    parts = trimmed.split(/\s+or\s+/);
  } else if (/\s+and\s+/.test(trimmed)) {
    parts = trimmed.split(/\s+and\s+/);
  } else {
    // Single filter, no logic separator
    parts = [trimmed];
  }

  // Sorted ops by label length desc for greedy matching
  const sortedOps = Object.entries(OP_LABELS)
    .sort((a, b) => b[1].length - a[1].length);

  const filters: StructuredFilter[] = [];
  for (const part of parts) {
    const p = part.trim();
    if (!p) continue;

    // Handle "limit N" special case
    const limitMatch = p.match(/^limit\s+(\d+)$/i);
    if (limitMatch) {
      filters.push({ field: LIMIT_FIELD, op: 'eq', value: limitMatch[1] });
      continue;
    }

    let matched = false;
    for (const [opKey, opLabel] of sortedOps) {
      const idx = p.indexOf(` ${opLabel} `);
      if (idx > 0) {
        const field = p.slice(0, idx).trim();
        const value = p.slice(idx + opLabel.length + 2).trim();
        if (field && value) {
          filters.push({ field, op: opKey, value });
          matched = true;
          break;
        }
      }
    }
    if (!matched) {
      // Fallback: split field op value by spaces
      const tokens = p.split(/\s+/);
      if (tokens.length >= 3) {
        const field = tokens[0];
        const opLabel = tokens[1];
        const opKey = OP_FROM_LABEL[opLabel] ?? opLabel;
        const value = tokens.slice(2).join(' ');
        filters.push({ field, op: opKey, value });
      } else if (tokens.length === 2) {
        // "field value" — assume eq
        filters.push({ field: tokens[0], op: 'eq', value: tokens[1] });
      }
    }
  }

  return filters.length > 0 ? { filters, logic } : null;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function SearchBar({
  filters, onChange, logic, onLogicChange, schema,
  onExecute, onCancel, loading,
}: SearchBarProps) {
  const [inputValue, setInputValue] = useState('');
  const [completionState, setCompletionState] = useState<CompletionState>('idle');
  const [pendingField, setPendingField] = useState<SchemaField | null>(null);
  const [pendingOp, setPendingOp] = useState<string | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [editTarget, setEditTarget] = useState<EditTarget>(null);
  const lastEnterTime = useRef(0);

  const inputRef = useRef<HTMLInputElement>(null);
  const editInputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const suggestionsRef = useRef<HTMLDivElement>(null);

  /** The currently active input — either the main one or the inline edit input */
  const activeInput = editTarget ? editInputRef : inputRef;

  // Close on outside click
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setShowSuggestions(false);
        setShowHistory(false);
        if (editTarget) cancelEditRef.current();
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [editTarget]);

  // ---------------------------------------------------------------------------
  // Inline editing — edit happens inside the chip
  // ---------------------------------------------------------------------------

  const cancelEdit = useCallback(() => {
    setEditTarget(null);
    setInputValue('');
    setCompletionState('idle');
    setPendingField(null);
    setPendingOp(null);
    setShowSuggestions(false);
    setActiveIndex(0);
  }, []);
  const cancelEditRef = useRef(cancelEdit);
  cancelEditRef.current = cancelEdit;

  const startEdit = useCallback((index: number, part: 'field' | 'op' | 'value') => {
    cancelEdit();
    const f = filters[index];
    setEditTarget({ index, part });

    if (part === 'field') {
      setInputValue(f.field);
      setCompletionState('field');
    } else if (part === 'op') {
      const field = schema?.fields.find((sf) => sf.name === f.field) ?? null;
      setPendingField(field);
      setInputValue(OP_LABELS[f.op] ?? f.op);
      setCompletionState('op');
    } else {
      const field = schema?.fields.find((sf) => sf.name === f.field) ?? null;
      setPendingField(field);
      setPendingOp(f.op);
      setInputValue(String(f.value));
      setCompletionState('value');
    }

    setShowSuggestions(true);
    setActiveIndex(0);
    // Focus the inline input after render
    setTimeout(() => { editInputRef.current?.focus(); editInputRef.current?.select(); }, 0);
  }, [filters, schema, cancelEdit]);

  const commitEditField = useCallback((field: SchemaField) => {
    if (!editTarget || editTarget.part !== 'field') return;
    const updated = [...filters];
    updated[editTarget.index] = { ...updated[editTarget.index], field: field.name };
    onChange(updated);
    cancelEdit();
    inputRef.current?.focus();
  }, [editTarget, filters, onChange, cancelEdit]);

  const commitEditOp = useCallback((op: string) => {
    if (!editTarget || editTarget.part !== 'op') return;
    const updated = [...filters];
    updated[editTarget.index] = { ...updated[editTarget.index], op };
    onChange(updated);
    cancelEdit();
    inputRef.current?.focus();
  }, [editTarget, filters, onChange, cancelEdit]);

  const commitEditValue = useCallback((value: string) => {
    if (!editTarget || editTarget.part !== 'value') return;
    const trimmed = value.trim();
    if (!trimmed) { cancelEdit(); return; }
    const updated = [...filters];
    updated[editTarget.index] = { ...updated[editTarget.index], value: trimmed };
    onChange(updated);
    cancelEdit();
    inputRef.current?.focus();
  }, [editTarget, filters, onChange, cancelEdit]);

  // ---------------------------------------------------------------------------
  // Suggestions
  // ---------------------------------------------------------------------------

  const suggestions: SuggestionItem[] = useMemo(() => {
    if (!schema) return [];
    const query = inputValue.toLowerCase();

    if (completionState === 'idle' || completionState === 'field') {
      const sorted = schema.fields
        .filter((f) => !query || f.name.toLowerCase().includes(query) || f.description.toLowerCase().includes(query))
        .sort((a, b) => {
          const [ga, sa] = rankField(a);
          const [gb, sb] = rankField(b);
          if (ga !== gb) return ga - gb;
          if (ga === -1) return sa - sb; // aliases: use explicit ordering
          return a.name.localeCompare(b.name); // non-aliases: alphabetical within group
        });

      // Add group headers
      const items: SuggestionItem[] = [];
      let lastGroup = '';
      for (const f of sorted) {
        const group = ALIASES.has(f.name) ? 'Aliases' : 'Fields';
        items.push({
          key: f.name,
          label: f.name,
          detail: f.description,
          data: f,
          group: group !== lastGroup ? group : undefined,
        });
        lastGroup = group;
      }

      // Inject "limit" pseudo-field at the end of aliases
      const limitLabel = LIMIT_FIELD;
      if (!query || limitLabel.includes(query)) {
        const limitField: SchemaField = { name: LIMIT_FIELD, filter_type: 'numeric', data_type: 'limit', description: 'Limit result rows' };
        // Insert after aliases, before Fields header
        const fieldsIdx = items.findIndex((it) => it.group === 'Fields');
        const limitItem: SuggestionItem = { key: LIMIT_FIELD, label: LIMIT_FIELD, detail: 'Limit result rows', data: limitField };
        if (fieldsIdx >= 0) items.splice(fieldsIdx, 0, limitItem);
        else items.push(limitItem);
      }

      return items;
    }

    if (completionState === 'op' && (pendingField || editTarget?.part === 'op')) {
      const ft = pendingField?.filter_type ?? '';
      const ops = schema.filter_types[ft] ?? [];
      return ops
        .filter((op) => {
          if (!query) return true;
          const label = OP_LABELS[op] ?? op;
          return label.toLowerCase().includes(query) || op.toLowerCase().includes(query);
        })
        .map((op) => ({ key: op, label: OP_LABELS[op] ?? op, detail: op, data: op }));
    }

    if (completionState === 'value' && (pendingField || editTarget?.part === 'value')) {
      const ft = pendingField?.filter_type ?? '';
      const op = pendingOp ?? '';
      if (ft === 'protocol' && op === 'named') {
        return COMMON_PROTOCOLS
          .filter((p) => !query || p.includes(query))
          .map((p) => ({ key: p, label: p, detail: 'protocol', data: p }));
      }
      if (ft === 'port' && op === 'named') {
        return COMMON_SERVICES
          .filter((s) => !query || s.includes(query))
          .map((s) => ({ key: s, label: s, detail: 'service', data: s }));
      }
    }

    return [];
  }, [schema, inputValue, completionState, pendingField, pendingOp, editTarget]);

  useEffect(() => {
    setActiveIndex((prev) => Math.min(prev, Math.max(0, suggestions.length - 1)));
  }, [suggestions.length]);

  useEffect(() => {
    if (!suggestionsRef.current) return;
    const active = suggestionsRef.current.querySelector('.search-suggestion.active');
    if (active) active.scrollIntoView({ block: 'nearest' });
  }, [activeIndex]);

  // ---------------------------------------------------------------------------
  // History
  // ---------------------------------------------------------------------------

  const history = useMemo(() => loadHistory(), [showHistory]); // eslint-disable-line react-hooks/exhaustive-deps

  const applyHistory = useCallback((entry: HistoryEntry) => {
    onChange(entry.filters);
    onLogicChange(entry.logic);
    setShowHistory(false);
    inputRef.current?.focus();
  }, [onChange, onLogicChange]);

  const saveCurrentToHistory = useCallback(() => {
    if (filters.length > 0) saveHistory({ filters, logic });
  }, [filters, logic]);

  // ---------------------------------------------------------------------------
  // Copy / Paste
  // ---------------------------------------------------------------------------

  const [copied, setCopied] = useState(false);
  const copyQueryToClipboard = useCallback(() => {
    const text = filtersToText(filters, logic);
    if (!text) return;
    // Try modern API first, fall back to execCommand
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }).catch(() => {
        // Fallback
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      });
    } else {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  }, [filters, logic]);

  const handlePaste = useCallback((e: React.ClipboardEvent) => {
    if (editTarget) return;
    if (completionState !== 'idle' && completionState !== 'field') return;
    const text = e.clipboardData.getData('text/plain');
    const parsed = textToFilters(text);
    if (parsed) {
      e.preventDefault();
      onChange(parsed.filters);
      onLogicChange(parsed.logic);
      setInputValue('');
      setCompletionState('idle');
      setShowSuggestions(false);
    }
  }, [editTarget, completionState, onChange, onLogicChange]);

  // ---------------------------------------------------------------------------
  // Composition actions (new filters)
  // ---------------------------------------------------------------------------

  const resetComposition = useCallback(() => {
    setInputValue('');
    setCompletionState('idle');
    setPendingField(null);
    setPendingOp(null);
    setEditTarget(null);
    setActiveIndex(0);
    setShowSuggestions(false);
  }, []);

  const selectField = useCallback((field: SchemaField) => {
    if (editTarget?.part === 'field') { commitEditField(field); return; }
    setPendingField(field);
    setInputValue('');
    // Limit skips op — goes straight to value
    if (field.name === LIMIT_FIELD) {
      setPendingOp('eq');
      setCompletionState('value');
    } else {
      setCompletionState('op');
    }
    setActiveIndex(0);
    setShowSuggestions(true);
  }, [editTarget, commitEditField]);

  const selectOp = useCallback((op: string) => {
    if (editTarget?.part === 'op') { commitEditOp(op); return; }
    setPendingOp(op);
    setInputValue('');
    setCompletionState('value');
    setActiveIndex(0);
    setShowSuggestions(true);
  }, [editTarget, commitEditOp]);

  const commitFilter = useCallback((value: string) => {
    if (editTarget?.part === 'value') { commitEditValue(value); return; }
    if (!pendingField || !pendingOp) return;
    const trimmed = value.trim();
    if (!trimmed) return;
    onChange([...filters, { field: pendingField.name, op: pendingOp, value: trimmed }]);
    resetComposition();
    inputRef.current?.focus();
  }, [editTarget, commitEditValue, pendingField, pendingOp, filters, onChange, resetComposition]);

  const removeFilter = useCallback((index: number) => {
    onChange(filters.filter((_, i) => i !== index));
    cancelEdit();
    inputRef.current?.focus();
  }, [filters, onChange, cancelEdit]);

  const selectSuggestion = useCallback((index: number) => {
    const item = suggestions[index];
    if (!item) return;
    if (completionState === 'idle' || completionState === 'field') selectField(item.data as SchemaField);
    else if (completionState === 'op') selectOp(item.data as string);
    else if (completionState === 'value') commitFilter(item.data as string);
  }, [suggestions, completionState, selectField, selectOp, commitFilter]);

  // ---------------------------------------------------------------------------
  // Keyboard — shared between main input and inline edit input
  // ---------------------------------------------------------------------------

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      if (editTarget && completionState === 'value' && inputValue.trim()) commitEditValue(inputValue);
      else if (completionState === 'value' && inputValue.trim()) commitFilter(inputValue);
      saveCurrentToHistory();
      onExecute();
      return;
    }

    if (e.key === 'Escape') {
      e.preventDefault();
      if (editTarget) { cancelEdit(); inputRef.current?.focus(); return; }
      if (showSuggestions || showHistory) { setShowSuggestions(false); setShowHistory(false); }
      else resetComposition();
      return;
    }

    if (e.key === 'Backspace' && inputValue === '') {
      e.preventDefault();
      if (editTarget) { cancelEdit(); inputRef.current?.focus(); return; }
      if (completionState === 'value') {
        setCompletionState('op'); setPendingOp(null); setShowSuggestions(true); setActiveIndex(0);
      } else if (completionState === 'op') {
        setCompletionState('idle'); setPendingField(null); setShowSuggestions(true); setActiveIndex(0);
      } else if (filters.length > 0) {
        removeFilter(filters.length - 1);
      }
      return;
    }

    if ((showSuggestions && suggestions.length > 0) || showHistory) {
      const listLen = showHistory ? history.length : suggestions.length;
      if (e.key === 'ArrowDown') { e.preventDefault(); setActiveIndex((p) => (p + 1) % listLen); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); setActiveIndex((p) => (p - 1 + listLen) % listLen); return; }
    }

    if (showHistory && (e.key === 'Tab' || e.key === 'Enter') && history.length > 0) {
      e.preventDefault(); applyHistory(history[activeIndex]); return;
    }

    if (showSuggestions && suggestions.length > 0 && (e.key === 'Tab' || e.key === 'Enter')) {
      e.preventDefault(); selectSuggestion(activeIndex); return;
    }

    if ((e.key === 'Enter' || e.key === 'Tab') && completionState === 'value' && inputValue.trim()) {
      e.preventDefault(); commitFilter(inputValue); return;
    }

    // Double-Enter in idle → execute
    if (e.key === 'Enter' && completionState === 'idle' && !inputValue.trim() && !showSuggestions && !editTarget) {
      e.preventDefault();
      const now = Date.now();
      if (now - lastEnterTime.current < 500) {
        saveCurrentToHistory(); onExecute(); lastEnterTime.current = 0;
      } else { lastEnterTime.current = now; }
    }
  }, [
    inputValue, showSuggestions, showHistory, suggestions, history, activeIndex,
    completionState, filters, editTarget, commitFilter, commitEditValue, onExecute,
    onCancel, removeFilter, resetComposition, cancelEdit, selectSuggestion,
    applyHistory, saveCurrentToHistory,
  ]);

  const handleInputChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const val = e.target.value;
    setInputValue(val);
    setActiveIndex(0);
    setShowHistory(false);
    if (completionState === 'idle' && val.length > 0) {
      setCompletionState('field');
      setShowSuggestions(true);
    } else if (val.length > 0) {
      setShowSuggestions(true);
    }
  }, [completionState]);

  const handleInputFocus = useCallback(() => {
    if (editTarget) { setShowSuggestions(true); return; }
    if (completionState === 'idle' || completionState === 'field') {
      setCompletionState(inputValue ? 'field' : 'idle');
      setShowSuggestions(true);
    } else {
      setShowSuggestions(true);
    }
  }, [completionState, inputValue, editTarget]);

  const handleSuggestionClick = useCallback((index: number) => {
    selectSuggestion(index);
    activeInput.current?.focus();
  }, [selectSuggestion, activeInput]);

  const handleContainerClick = useCallback(() => {
    if (!editTarget) inputRef.current?.focus();
  }, [editTarget]);

  // Placeholder
  const placeholder = useMemo(() => {
    if (completionState === 'op' && pendingField) return `operator for ${pendingField.name}...`;
    if (completionState === 'value' && pendingField && pendingOp)
      return `value for ${pendingField.name} ${OP_LABELS[pendingOp] ?? pendingOp}...`;
    if (filters.length === 0) return 'Type to add filters (e.g. src, proto, dport)...';
    return 'Add filter...';
  }, [completionState, pendingField, pendingOp, filters.length]);

  // ---------------------------------------------------------------------------
  // Render chip — with inline edit support
  // ---------------------------------------------------------------------------

  const renderChipPart = useCallback(
    (text: string, cls: string, editable: boolean, index: number | null, part: 'field' | 'op' | 'value') => {
      const isEditing = editTarget && index !== null && editTarget.index === index && editTarget.part === part;

      if (isEditing) {
        // Measure: at least as wide as the original text, grows with typed text
        const charWidth = part === 'op' ? 0.65 : 0.62; // em per char approximation
        const chars = Math.max(inputValue.length + 1, text.length, 6);
        return (
          <span className={`search-chip-edit-wrap ${cls}`}>
            <input
              ref={editInputRef}
              className="search-chip-edit-input"
              value={inputValue}
              onChange={handleInputChange}
              onKeyDown={handleKeyDown}
              onFocus={handleInputFocus}
              spellCheck={false}
              autoComplete="off"
              style={{ width: `${chars * charWidth}em` }}
            />
          </span>
        );
      }

      return (
        <span
          className={`${cls}${editable ? ' editable' : ''}`}
          onClick={editable && index !== null ? (e) => { e.stopPropagation(); startEdit(index, part); } : undefined}
        >
          {text}
        </span>
      );
    },
    [editTarget, inputValue, handleInputChange, handleKeyDown, handleInputFocus, startEdit],
  );

  const renderChip = useCallback((f: StructuredFilter, index: number | null, editable: boolean) => {
    if (isLimitFilter(f)) {
      // Limit: 2-part chip (label + value), amber/yellow colored
      return (
        <span className="search-chip search-chip-limit">
          <span className="search-chip-limit-label">limit</span>
          {renderChipPart(String(f.value), 'search-chip-limit-value', editable, index, 'value')}
          {editable && index !== null && (
            <button className="search-chip-remove"
              onClick={(e) => { e.stopPropagation(); removeFilter(index); }}>
              {'\u00D7'}
            </button>
          )}
        </span>
      );
    }
    return (
      <span className="search-chip">
        {renderChipPart(f.field, 'search-chip-field', editable, index, 'field')}
        {renderChipPart(OP_LABELS[f.op] ?? f.op, 'search-chip-op', editable, index, 'op')}
        {renderChipPart(String(f.value), 'search-chip-value', editable, index, 'value')}
        {editable && index !== null && (
          <button className="search-chip-remove"
            onClick={(e) => { e.stopPropagation(); removeFilter(index); }}>
            {'\u00D7'}
          </button>
        )}
      </span>
    );
  }, [renderChipPart, removeFilter]);

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <div className="search-bar" ref={containerRef}>
      <div className="search-bar-inner" onClick={handleContainerClick}>
        <div className="search-bar-chips">
          {filters.map((f, i) => (
            <span key={i} className="search-chip-group">
              {i > 0 && (
                <button className="search-logic-toggle"
                  onClick={(e) => { e.stopPropagation(); onLogicChange(logic === 'and' ? 'or' : 'and'); }}
                  title="Toggle AND/OR">
                  {logic}
                </button>
              )}
              {renderChip(f, i, true)}
            </span>
          ))}

          {pendingField && !editTarget && (
            <span className="search-chip search-chip-pending">
              <span className="search-chip-field">{pendingField.name}</span>
              {pendingOp && <span className="search-chip-op">{OP_LABELS[pendingOp] ?? pendingOp}</span>}
            </span>
          )}

          {/* Main input — hidden when editing inline */}
          <input
            ref={inputRef}
            className="search-input"
            type="text"
            value={editTarget ? '' : inputValue}
            onChange={editTarget ? undefined : handleInputChange}
            onKeyDown={editTarget ? undefined : handleKeyDown}
            onFocus={editTarget ? undefined : handleInputFocus}
            onPaste={editTarget ? undefined : handlePaste}
            placeholder={editTarget ? '' : placeholder}
            spellCheck={false}
            autoComplete="off"
            style={editTarget ? { width: 0, minWidth: 0, padding: 0, opacity: 0 } : undefined}
          />
        </div>

        <div className="search-bar-actions">
          {filters.length > 0 && (
            <button className={`search-copy-btn${copied ? ' copied' : ''}`}
              onClick={(e) => { e.stopPropagation(); copyQueryToClipboard(); }}
              title={copied ? 'Copied!' : 'Copy query to clipboard'}>
              {copied ? (
                <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
                  <path d="M13.78 4.22a.75.75 0 010 1.06l-7.25 7.25a.75.75 0 01-1.06 0L2.22 9.28a.75.75 0 011.06-1.06L6 10.94l6.72-6.72a.75.75 0 011.06 0z"/>
                </svg>
              ) : (
                <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
                  <path d="M0 6.75C0 5.784.784 5 1.75 5h1.5a.75.75 0 010 1.5h-1.5a.25.25 0 00-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 00.25-.25v-1.5a.75.75 0 011.5 0v1.5A1.75 1.75 0 019.25 16h-7.5A1.75 1.75 0 010 14.25v-7.5z"/>
                  <path d="M5 1.75C5 .784 5.784 0 6.75 0h7.5C15.216 0 16 .784 16 1.75v7.5A1.75 1.75 0 0114.25 11h-7.5A1.75 1.75 0 015 9.25v-7.5zm1.75-.25a.25.25 0 00-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 00.25-.25v-7.5a.25.25 0 00-.25-.25h-7.5z"/>
                </svg>
              )}
            </button>
          )}
          <button className="search-history-btn"
            onClick={(e) => { e.stopPropagation(); setShowHistory(!showHistory); setShowSuggestions(false); setActiveIndex(0); }}
            title="Query history">
            <svg width="15" height="15" viewBox="0 0 16 16" fill="currentColor">
              <path d="M8 1a7 7 0 11-4.95 2.05l.71.71A6 6 0 108 2V1zm0 3v4.5l3.5 2.1-.5.87L7 9V4h1z"/>
            </svg>
          </button>
          <button className={`search-run-btn${loading ? ' loading' : ''}`}
            onClick={(e) => {
              e.stopPropagation();
              if (loading) onCancel(); else { saveCurrentToHistory(); onExecute(); }
            }}
            title={loading ? 'Cancel query' : 'Run query (Enter Enter)'}>
            {loading ? (
              <span className="search-run-spinner" />
            ) : (
              <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
                <path d="M4 2l10 6-10 6V2z"/>
              </svg>
            )}
          </button>
        </div>
      </div>

      {/* Suggestions with group headers */}
      {showSuggestions && suggestions.length > 0 && !showHistory && (
        <div className="search-suggestions" ref={suggestionsRef}>
          {completionState !== 'field' && completionState !== 'idle' && (
            <div className="search-suggestions-header">
              {completionState === 'op' ? 'Operators' : 'Values'}
            </div>
          )}
          {suggestions.map((s, i) => (
            <div key={s.key}>
              {s.group && <div className="search-suggestions-header">{s.group}</div>}
              <div
                className={`search-suggestion${i === activeIndex ? ' active' : ''}`}
                onMouseDown={(e) => { e.preventDefault(); handleSuggestionClick(i); }}
                onMouseEnter={() => setActiveIndex(i)}
              >
                <span className="search-suggestion-label">{s.label}</span>
                {s.detail && <span className="search-suggestion-detail">{s.detail}</span>}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* History */}
      {showHistory && history.length > 0 && (
        <div className="search-suggestions" ref={suggestionsRef}>
          <div className="search-suggestions-header">Recent queries</div>
          {history.map((entry, i) => (
            <div key={i}
              className={`search-suggestion search-suggestion-history${i === activeIndex ? ' active' : ''}`}
              onMouseDown={(e) => { e.preventDefault(); applyHistory(entry); }}
              onMouseEnter={() => setActiveIndex(i)}>
              <span className="search-suggestion-label">
                {entry.filters.map((f, j) => (
                  <span key={j} className="history-filter-group">
                    {j > 0 && <span className="history-logic">{entry.logic}</span>}
                    {renderChip(f, null, false)}
                  </span>
                ))}
                {entry.filters.length === 0 && <span className="history-empty">No filters</span>}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
