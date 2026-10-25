/**
 * Typed fetch client for the NUVEXA API.
 *
 * - Same-origin only (`/api/v1`), `credentials: "same-origin"` — the session cookie is HttpOnly.
 * - Double-submit CSRF: unsafe methods send `X-CSRF-Token` with the readable `nuvexa_csrf` cookie.
 * - Every request carries a generated `X-Request-ID`, surfaced in error UI for support.
 * - Every non-2xx response is normalised into an {@link ApiError} using the API error schema
 *   `{ error: { code, message, correlation_id, details } }`.
 * - 401 triggers the registered unauthorized handler (redirect to /login) unless opted out.
 */
import type { ErrorResponse } from './types';

export const API_BASE = '/api/v1';
export const CSRF_COOKIE = 'nuvexa_csrf';
export const CSRF_HEADER = 'X-CSRF-Token';
export const REQUEST_ID_HEADER = 'X-Request-ID';

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
const UNSAFE_METHODS: ReadonlySet<HttpMethod> = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

export type QueryScalar = string | number | boolean | null | undefined;
/** Arrays are sent as repeated parameters (`?status=a&status=b`). */
export type QueryValue = QueryScalar | readonly QueryScalar[];

export interface RequestOptions {
  method?: HttpMethod;
  body?: unknown;
  query?: Record<string, QueryValue>;
  signal?: AbortSignal;
  /** Do not invoke the unauthorized handler on 401 (used by the session probe and login page). */
  skipAuthRedirect?: boolean;
}

export interface FieldErrorDetail {
  field: string;
  message: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly correlationId: string | null;
  readonly requestId: string;
  readonly details: Record<string, unknown>[];

  constructor(init: {
    status: number;
    code: string;
    message: string;
    correlationId?: string | null;
    requestId: string;
    details?: Record<string, unknown>[];
  }) {
    super(init.message);
    this.name = 'ApiError';
    this.status = init.status;
    this.code = init.code;
    this.correlationId = init.correlationId ?? null;
    this.requestId = init.requestId;
    this.details = init.details ?? [];
  }

  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  get isForbidden(): boolean {
    return this.status === 403;
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }

  /** 409 `stale_version`: the resource changed since it was loaded — reload before acting. */
  get isStaleVersion(): boolean {
    return this.status === 409 && this.code === 'stale_version';
  }

  get isConflict(): boolean {
    return this.status === 409;
  }

  get isValidation(): boolean {
    return this.status === 422;
  }

  get isNetworkError(): boolean {
    return this.status === 0;
  }

  /** Support reference shown to users: the server correlation id, else our request id. */
  get supportReference(): string {
    return this.correlationId ?? this.requestId;
  }

  /** Field errors from a 422 `validation_failed` response. */
  get fieldErrors(): FieldErrorDetail[] {
    return parseFieldErrors(this.details);
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

// --- Unauthorized handling ---------------------------------------------------------------------

let redirecting = false;

function defaultUnauthorizedHandler(): void {
  if (typeof window === 'undefined' || redirecting) return;
  const { pathname, search } = window.location;
  if (pathname === '/login') return;
  redirecting = true;
  const target = `${pathname}${search}`;
  window.location.assign(`/login?redirect_to=${encodeURIComponent(target)}`);
}

let unauthorizedHandler: () => void = defaultUnauthorizedHandler;

/** Override the 401 behaviour (tests, or an in-app navigation). Returns a restore function. */
export function setUnauthorizedHandler(handler: () => void): () => void {
  const previous = unauthorizedHandler;
  unauthorizedHandler = handler;
  return () => {
    unauthorizedHandler = previous;
  };
}

// --- Helpers -------------------------------------------------------------------------------------

export function readCookie(name: string): string | null {
  if (typeof document === 'undefined' || !document.cookie) return null;
  for (const part of document.cookie.split(';')) {
    const index = part.indexOf('=');
    if (index === -1) continue;
    const key = part.slice(0, index).trim();
    if (key === name) {
      const raw = part.slice(index + 1).trim();
      try {
        return decodeURIComponent(raw);
      } catch {
        return raw;
      }
    }
  }
  return null;
}

export function generateRequestId(): string {
  const cryptoObj = globalThis.crypto as Crypto | undefined;
  if (cryptoObj && typeof cryptoObj.randomUUID === 'function') {
    return cryptoObj.randomUUID();
  }
  // Fallback for non-secure contexts (randomUUID requires https or localhost).
  const bytes = new Uint8Array(16);
  if (cryptoObj && typeof cryptoObj.getRandomValues === 'function') {
    cryptoObj.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  }
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

export function buildQuery(query: Record<string, QueryValue> | undefined): string {
  if (!query) return '';
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    const values: readonly QueryScalar[] = Array.isArray(value)
      ? (value as readonly QueryScalar[])
      : [value as QueryScalar];
    for (const item of values) {
      if (item === undefined || item === null || item === '') continue;
      params.append(key, String(item));
    }
  }
  const text = params.toString();
  return text ? `?${text}` : '';
}

function isErrorResponse(value: unknown): value is ErrorResponse {
  if (typeof value !== 'object' || value === null || !('error' in value)) return false;
  const err = value.error;
  return (
    typeof err === 'object' &&
    err !== null &&
    typeof (err as { code?: unknown }).code === 'string' &&
    typeof (err as { message?: unknown }).message === 'string'
  );
}

/**
 * Normalise `details` entries into `{field, message}`. The API may emit `{field, message}` or
 * FastAPI-style `{loc: [...], msg}` entries; both are accepted. Request-envelope prefixes
 * (`body`, `payload`, `settings`) are stripped so paths match form field names.
 */
export function parseFieldErrors(details: Record<string, unknown>[]): FieldErrorDetail[] {
  const out: FieldErrorDetail[] = [];
  for (const detail of details) {
    const message =
      typeof detail.message === 'string'
        ? detail.message
        : typeof detail.msg === 'string'
          ? detail.msg
          : null;
    if (!message) continue;
    let path: string[] = [];
    if (typeof detail.field === 'string') {
      path = detail.field.split('.');
    } else if (Array.isArray(detail.loc)) {
      path = detail.loc.map((part) => String(part));
    }
    while (path.length > 0 && ['body', 'payload', 'settings'].includes(path[0] ?? '')) {
      path = path.slice(1);
    }
    out.push({ field: path.join('.'), message });
  }
  return out;
}

async function parseBody(response: Response): Promise<unknown> {
  if (response.status === 204 || response.status === 205) return undefined;
  const text = await response.text();
  if (!text) return undefined;
  const contentType = response.headers.get('content-type') ?? '';
  if (contentType.includes('json')) {
    try {
      return JSON.parse(text) as unknown;
    } catch {
      return undefined;
    }
  }
  return text;
}

// --- Core request --------------------------------------------------------------------------------

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? 'GET';
  const requestId = generateRequestId();
  const headers = new Headers({ Accept: 'application/json', [REQUEST_ID_HEADER]: requestId });

  let body: string | undefined;
  if (options.body !== undefined) {
    headers.set('Content-Type', 'application/json');
    body = JSON.stringify(options.body);
  }
  if (UNSAFE_METHODS.has(method)) {
    const csrf = readCookie(CSRF_COOKIE);
    if (csrf) headers.set(CSRF_HEADER, csrf);
  }

  const url = `${API_BASE}${path}${buildQuery(options.query)}`;

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body,
      credentials: 'same-origin',
      signal: options.signal,
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw new ApiError({
      status: 0,
      code: 'network_error',
      message: 'Could not reach the NUVEXA API. Check your connection and try again.',
      requestId,
    });
  }

  const echoedRequestId = response.headers.get(REQUEST_ID_HEADER) ?? requestId;
  const payload = await parseBody(response);

  if (!response.ok) {
    const error = isErrorResponse(payload)
      ? new ApiError({
          status: response.status,
          code: payload.error.code,
          message: payload.error.message,
          correlationId: payload.error.correlation_id,
          details: Array.isArray(payload.error.details) ? payload.error.details : [],
          requestId: echoedRequestId,
        })
      : new ApiError({
          status: response.status,
          code: response.status === 401 ? 'authentication_required' : `http_${response.status}`,
          message: response.statusText || `Request failed with status ${response.status}.`,
          requestId: echoedRequestId,
        });
    if (response.status === 401 && !options.skipAuthRedirect) {
      unauthorizedHandler();
    }
    throw error;
  }

  return payload as T;
}

export const http = {
  get: <T>(path: string, options?: Omit<RequestOptions, 'method' | 'body'>) =>
    apiRequest<T>(path, { ...options, method: 'GET' }),
  post: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, 'method' | 'body'>) =>
    apiRequest<T>(path, { ...options, method: 'POST', body }),
  put: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, 'method' | 'body'>) =>
    apiRequest<T>(path, { ...options, method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, 'method' | 'body'>) =>
    apiRequest<T>(path, { ...options, method: 'PATCH', body }),
  delete: <T>(path: string, options?: Omit<RequestOptions, 'method' | 'body'>) =>
    apiRequest<T>(path, { ...options, method: 'DELETE' }),
};

/** Full-page navigation URL for browser-redirect flows (Microsoft sign-in / connect). */
export function navigationUrl(path: string, redirectTo: string): string {
  return `${API_BASE}${path}${buildQuery({ redirect_to: safeRedirectPath(redirectTo) })}`;
}

/**
 * Characters a same-app redirect path may contain: one leading "/" not followed by another, then the
 * backend's allow-list (`_SAFE_REDIRECT_RE` in app/services/auth_service.py) plus "#", "+" and the
 * other RFC 3986 sub-delimiters the URL parser leaves unencoded in a path or query. An allow-list,
 * not a deny-list: no whitespace or control characters (the URL parser strips tab/CR/LF anywhere,
 * so "/\t/evil.example" would become the protocol-relative "//evil.example"), no backslash (parsed
 * as "/"), no non-ASCII. Anything else falls back to the default path.
 */
const SAFE_REDIRECT_RE = /^\/(?!\/)[A-Za-z0-9/_\-?=&.%#+~!$'()*,;:@]*$/;

/** Only relative, same-app paths are accepted as post-login redirects. */
export function safeRedirectPath(value: string | null | undefined, fallback = '/'): string {
  const safe =
    typeof value === 'string' &&
    SAFE_REDIRECT_RE.test(value) &&
    !value.startsWith('/login') &&
    !value.startsWith('/api/') &&
    // Defence in depth: whatever the browser resolves the value to must stay on this origin.
    new URL(value, window.location.origin).origin === window.location.origin;
  return safe ? value : fallback;
}
