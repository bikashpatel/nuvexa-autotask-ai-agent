import { describe, expect, it } from 'vitest';

import { classifyUrl } from './urls';

describe('classifyUrl', () => {
  it.each([
    'https://dev.azure.com/contoso/Payments/_workitems/edit/4711',
    'http://localhost:8080/x',
    'HTTPS://EXAMPLE.COM',
    '  https://example.com/padded  ',
  ])('%j → http (may become a link)', (url) => {
    expect(classifyUrl(url)).toBe('http');
  });

  it.each(['mock://contoso/Payments/_workitems/edit/4711', 'MOCK://x', '  mock://padded'])(
    '%j → mock (text + badge, never a link)',
    (url) => {
      expect(classifyUrl(url)).toBe('mock');
    },
  );

  it.each([
    'javascript:alert(1)',
    'JavaScript:alert(1)',
    ' javascript:alert(1)',
    'data:text/html;base64,PHNjcmlwdD4=',
    'file:///etc/passwd',
    'vbscript:msgbox(1)',
    '//evil.example/x',
    '/relative/path',
    'ftp://example.com',
    'httpx://example.com',
    'https:/missing-slash',
    '',
  ])('%j → unsafe (plain text)', (url) => {
    expect(classifyUrl(url)).toBe('unsafe');
  });
});
