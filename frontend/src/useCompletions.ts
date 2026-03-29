import { useCallback, useEffect, useMemo, useState } from 'react';
import { fetchFields, type FieldInfo } from './api';

export interface Completion {
  label: string;
  kind: string;
  detail?: string;
}

// ── Completion pools ────────────────────────────

const DURATIONS: Completion[] = [
  { label: '5m', kind: 'unit', detail: '5 minutes' },
  { label: '15m', kind: 'unit', detail: '15 minutes' },
  { label: '1h', kind: 'unit', detail: '1 hour' },
  { label: '6h', kind: 'unit', detail: '6 hours' },
  { label: '24h', kind: 'unit', detail: '1 day' },
  { label: '7d', kind: 'unit', detail: '7 days' },
  { label: '30d', kind: 'unit', detail: '30 days' },
];

const FILTERS: Completion[] = [
  { label: 'src', kind: 'direction', detail: 'source IP' },
  { label: 'dst', kind: 'direction', detail: 'destination IP' },
  { label: 'sport', kind: 'direction', detail: 'source port' },
  { label: 'dport', kind: 'direction', detail: 'destination port' },
  { label: 'proto', kind: 'direction', detail: 'protocol' },
  { label: 'flags', kind: 'direction', detail: 'TCP flags' },
  { label: 'port', kind: 'direction', detail: 'any port' },
  { label: 'ip', kind: 'direction', detail: 'any IP' },
];

const PROTOCOLS: Completion[] = [
  { label: 'tcp', kind: 'value', detail: '6' },
  { label: 'udp', kind: 'value', detail: '17' },
  { label: 'icmp', kind: 'value', detail: '1' },
  { label: 'gre', kind: 'value', detail: '47' },
  { label: 'esp', kind: 'value', detail: '50' },
  { label: 'sctp', kind: 'value', detail: '132' },
  { label: 'ospf', kind: 'value', detail: '89' },
];

const PORTS: Completion[] = [
  { label: 'dns', kind: 'port', detail: '53' },
  { label: 'http', kind: 'port', detail: '80' },
  { label: 'https', kind: 'port', detail: '443' },
  { label: 'ssh', kind: 'port', detail: '22' },
  { label: 'ftp', kind: 'port', detail: '21' },
  { label: 'smtp', kind: 'port', detail: '25' },
  { label: 'ntp', kind: 'port', detail: '123' },
  { label: 'snmp', kind: 'port', detail: '161' },
  { label: 'syslog', kind: 'port', detail: '514' },
  { label: 'rdp', kind: 'port', detail: '3389' },
  { label: 'mysql', kind: 'port', detail: '3306' },
  { label: 'postgres', kind: 'port', detail: '5432' },
  { label: 'redis', kind: 'port', detail: '6379' },
  { label: 'bgp', kind: 'port', detail: '179' },
];

const AGGREGATIONS: Completion[] = [
  { label: 'top', kind: 'keyword', detail: 'top N by metric' },
  { label: 'bottom', kind: 'keyword', detail: 'bottom N by metric' },
  { label: 'group by', kind: 'keyword', detail: 'aggregate rows' },
  { label: 'select', kind: 'keyword', detail: 'choose columns' },
  { label: 'sort', kind: 'keyword', detail: 'order results' },
  { label: 'limit', kind: 'keyword', detail: 'max rows' },
];

const AGG_FNS: Completion[] = [
  { label: 'sum', kind: 'function', detail: 'total' },
  { label: 'avg', kind: 'function', detail: 'average' },
  { label: 'count', kind: 'function', detail: 'count rows' },
  { label: 'min', kind: 'function', detail: 'minimum' },
  { label: 'max', kind: 'function', detail: 'maximum' },
  { label: 'uniq', kind: 'function', detail: 'unique count' },
  { label: 'p50', kind: 'function', detail: 'median' },
  { label: 'p95', kind: 'function', detail: '95th pct' },
  { label: 'p99', kind: 'function', detail: '99th pct' },
  { label: 'stddev', kind: 'function', detail: 'std deviation' },
  { label: 'rate', kind: 'function', detail: 'per second' },
];

const PIPE: Completion = { label: '|', kind: 'operator', detail: 'next stage' };
const LOGICAL: Completion[] = [
  { label: 'and', kind: 'operator' },
  { label: 'or', kind: 'operator' },
  { label: 'not', kind: 'operator' },
];

const KNOWN_VALUES = new Set([
  'tcp', 'udp', 'icmp', 'gre', 'esp', 'sctp', 'ospf', 'eigrp',
  'dns', 'http', 'https', 'ssh', 'ftp', 'smtp', 'ntp', 'snmp',
  'syslog', 'rdp', 'mysql', 'postgres', 'redis', 'bgp', 'ldap',
]);

const FILTER_WORDS = new Set([
  'src', 'dst', 'ip', 'port', 'sport', 'dport', 'proto', 'flags',
]);

// ── Query structure analysis ────────────────────

/**
 * Analyze the query text before cursor to determine what to suggest.
 * Returns a ranked list of completions for the current position.
 */
function analyzeQuery(
  textBeforeCursor: string,
  word: string,
  fields: Completion[],
): Completion[] {
  const text = textBeforeCursor.trimEnd();
  if (text.length === 0 && word.length === 0) {
    // Empty — start with time range
    return [
      { label: 'last', kind: 'keyword', detail: 'time range' },
      PIPE,
      ...AGGREGATIONS,
      ...FILTERS,
    ];
  }

  // Check parentheses balance — inside a function call?
  const opens = (text.match(/\(/g) || []).length;
  const closes = (text.match(/\)/g) || []).length;
  if (opens > closes) {
    // Inside fn() — suggest fields
    return fields;
  }

  // Split by pipes to understand pipeline stages
  const pipes = text.split('|');
  const currentStage = pipes[pipes.length - 1].trimStart();
  const tokens = currentStage.split(/\s+/).filter(Boolean);
  const lastToken = tokens[tokens.length - 1]?.toLowerCase() ?? '';
  const prevToken = tokens.length > 1 ? tokens[tokens.length - 2]?.toLowerCase() : '';

  // Right after a pipe character
  if (text.endsWith('|') || currentStage.length === 0) {
    // After pipe: aggregation/pipeline ops first, then filters
    return [...AGGREGATIONS, ...FILTERS, ...fields];
  }

  // After "last" keyword — suggest durations
  if (lastToken === 'last') return DURATIONS;

  // After "at" keyword — date hint
  if (lastToken === 'at') return [{ label: '2024-01-01', kind: 'hint', detail: 'date' }];

  // After a duration (e.g. "last 1h") — suggest pipe to next stage
  if (prevToken === 'last' || prevToken === 'at' || prevToken === 'offset') {
    return [PIPE, ...AGGREGATIONS, ...FILTERS];
  }

  // After "src", "dst", "ip" — no completions, user types IP
  if (lastToken === 'src' || lastToken === 'dst' || lastToken === 'ip') return [];

  // After "proto" — suggest protocol names
  if (lastToken === 'proto') return PROTOCOLS;

  // After "sport", "dport", "port" — suggest named ports
  if (lastToken === 'sport' || lastToken === 'dport' || lastToken === 'port') return PORTS;

  // After "group" — suggest "by"
  if (lastToken === 'group') return [{ label: 'by', kind: 'keyword' }];

  // After "by" — suggest fields and agg functions
  if (lastToken === 'by') return [...AGG_FNS, ...fields];

  // After "top" or "bottom" — suggest numbers
  if (lastToken === 'top' || lastToken === 'bottom') {
    return [
      { label: '10', kind: 'hint', detail: 'count' },
      { label: '25', kind: 'hint', detail: 'count' },
      { label: '50', kind: 'hint', detail: 'count' },
      { label: '100', kind: 'hint', detail: 'count' },
    ];
  }

  // After "top N" — suggest "by"
  if (prevToken === 'top' || prevToken === 'bottom') {
    return [{ label: 'by', kind: 'keyword', detail: 'followed by metric' }];
  }

  // After "select" — suggest fields
  if (lastToken === 'select') return [{ label: '*', kind: 'operator', detail: 'all columns' }, ...fields];

  // After "sort" — suggest fields and agg functions
  if (lastToken === 'sort') return [...AGG_FNS, ...fields];

  // After "and", "or", "not" — back to filters
  if (lastToken === 'and' || lastToken === 'or' || lastToken === 'not') return [...FILTERS, ...fields];

  // After a value (number, IP, protocol name, port name) — suggest pipe or logical ops
  if (/^[0-9]/.test(lastToken) || lastToken.includes('/') || lastToken.includes('.') || KNOWN_VALUES.has(lastToken)) {
    return [PIPE, ...LOGICAL, ...AGGREGATIONS];
  }

  // After a comparison operator
  if (['>', '<', '>=', '<=', '=', '!='].includes(lastToken)) return [];

  // After a filter direction followed by a value (2 tokens ago was filter word)
  const twoBack = tokens.length > 2 ? tokens[tokens.length - 3]?.toLowerCase() : '';
  if (FILTER_WORDS.has(twoBack) || FILTER_WORDS.has(prevToken ?? '')) {
    // We're after "src 10.0.0.1" or similar
    if (!FILTER_WORDS.has(lastToken) && !['and', 'or', 'not', 'by', '|'].includes(lastToken)) {
      return [PIPE, ...LOGICAL, ...AGGREGATIONS];
    }
  }

  // After a closing paren — likely after agg function
  if (lastToken.endsWith(')')) {
    return [
      { label: 'asc', kind: 'keyword', detail: 'ascending' },
      { label: 'desc', kind: 'keyword', detail: 'descending' },
      PIPE,
    ];
  }

  // Default: offer everything useful
  return [PIPE, ...AGGREGATIONS, ...FILTERS, ...AGG_FNS, ...fields];
}

function getCurrentWord(text: string, cursorPos: number): { word: string; start: number } {
  let start = cursorPos;
  while (start > 0 && /[a-zA-Z0-9_.]/.test(text[start - 1])) {
    start--;
  }
  return { word: text.slice(start, cursorPos), start };
}

export function useCompletions() {
  const [remoteFields, setRemoteFields] = useState<FieldInfo[]>([]);

  useEffect(() => {
    fetchFields().then(setRemoteFields).catch(() => {});
  }, []);

  const fieldCompletions = useMemo<Completion[]>(() =>
    remoteFields.map((f) => ({
      label: f.name,
      kind: 'field',
      detail: f.description || f.type,
    })),
    [remoteFields],
  );

  const getCompletions = useCallback(
    (text: string, cursorPos: number): { items: Completion[]; wordStart: number } => {
      const { word, start } = getCurrentWord(text, cursorPos);
      const textBefore = text.slice(0, start);
      const pool = analyzeQuery(textBefore, word, fieldCompletions);

      if (word.length === 0) {
        // No typing — show top context suggestions
        return { items: pool.slice(0, 12), wordStart: start };
      }

      // Filter by prefix
      const lower = word.toLowerCase();
      const items = pool.filter((c) =>
        c.label.toLowerCase().startsWith(lower) && c.label.toLowerCase() !== lower,
      );

      // Deduplicate
      const seen = new Set<string>();
      const unique = items.filter((c) => {
        if (seen.has(c.label)) return false;
        seen.add(c.label);
        return true;
      });

      return { items: unique.slice(0, 15), wordStart: start };
    },
    [fieldCompletions],
  );

  return { getCompletions };
}
