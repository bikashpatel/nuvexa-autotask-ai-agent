import { createContext, useContext } from 'react';
import { useQuery } from '@tanstack/react-query';

import { authApi } from '../../api/endpoints';
import { queryKeys } from '../../api/queryKeys';
import type { MeOut, Permission } from '../../api/types';

export const SessionContext = createContext<MeOut | null>(null);

/** Current user + permissions. Only usable below <RequireAuth>. */
export function useSession(): MeOut {
  const session = useContext(SessionContext);
  if (!session) throw new Error('useSession must be used inside <RequireAuth>');
  return session;
}

export function useHasPermission(permission: Permission): boolean {
  const session = useContext(SessionContext);
  return session?.permissions.includes(permission) ?? false;
}

export function useMeQuery() {
  return useQuery({
    queryKey: queryKeys.me,
    queryFn: authApi.me,
    retry: false,
    staleTime: 60_000,
  });
}

export function useAuthConfig() {
  return useQuery({
    queryKey: queryKeys.authConfig,
    queryFn: authApi.config,
    staleTime: 5 * 60_000,
  });
}
