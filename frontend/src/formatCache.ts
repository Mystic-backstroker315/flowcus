/** Simple LRU cache for formatted values. */

export class FormatCache {
  private cache = new Map<string, string>();
  private readonly maxSize: number;

  constructor(maxSize = 12288) {
    this.maxSize = maxSize;
  }

  get(key: string): string | undefined {
    const val = this.cache.get(key);
    if (val !== undefined) {
      // Move to end (most recently used)
      this.cache.delete(key);
      this.cache.set(key, val);
    }
    return val;
  }

  set(key: string, value: string): void {
    if (this.cache.has(key)) {
      this.cache.delete(key);
    } else if (this.cache.size >= this.maxSize) {
      // Evict oldest (first entry)
      const first = this.cache.keys().next().value;
      if (first !== undefined) this.cache.delete(first);
    }
    this.cache.set(key, value);
  }

  clear(): void {
    this.cache.clear();
  }
}

// Singleton cache shared across all formatters
export const formatCache = new FormatCache();

/** Wrap a formatter function with LRU caching. */
export function cached(
  columnName: string,
  fn: (value: unknown) => string,
): (value: unknown) => string {
  return (value: unknown) => {
    if (value === null || value === undefined) return '\u2014';
    const key = `${columnName}:${String(value)}`;
    const hit = formatCache.get(key);
    if (hit !== undefined) return hit;
    const result = fn(value);
    formatCache.set(key, result);
    return result;
  };
}
