import { QueryClient } from '@tanstack/react-query';

import { isApiError } from '../api/client';

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        refetchOnWindowFocus: true,
        retry: (failureCount, error) => {
          // Client errors (401/403/404/409/422…) are deterministic: do not retry them.
          if (isApiError(error) && error.status >= 400 && error.status < 500) return false;
          return failureCount < 2;
        },
      },
      mutations: {
        // Writes are never retried automatically (approvals/executions must not be replayed).
        retry: false,
      },
    },
  });
}
