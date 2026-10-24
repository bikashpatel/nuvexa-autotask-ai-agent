import { Center, Container, Loader, Stack, Text } from '@mantine/core';
import { Navigate, Outlet, useLocation } from 'react-router';

import { isApiError } from '../../api/client';
import { ErrorState } from '../../components/ErrorState';
import { SessionContext, useMeQuery } from './session';

export function FullPageLoader({ label = 'Loading NUVEXA…' }: { label?: string }) {
  return (
    <Center mih="100vh">
      <Stack align="center" gap="sm" role="status" aria-live="polite">
        <Loader />
        <Text size="sm" c="dimmed">
          {label}
        </Text>
      </Stack>
    </Center>
  );
}

/** Route guard: probes the session and provides it to the protected tree. */
export function RequireAuth() {
  const location = useLocation();
  const me = useMeQuery();

  if (me.isPending) return <FullPageLoader label="Checking your session…" />;

  if (me.isError) {
    if (isApiError(me.error) && me.error.isUnauthorized) {
      const target = `${location.pathname}${location.search}`;
      return <Navigate to={`/login?redirect_to=${encodeURIComponent(target)}`} replace />;
    }
    return (
      <Container size="sm" py="xl">
        <ErrorState
          error={me.error}
          title="Could not load your session"
          onRetry={() => void me.refetch()}
          retrying={me.isFetching}
        />
      </Container>
    );
  }

  return (
    <SessionContext.Provider value={me.data}>
      <Outlet />
    </SessionContext.Provider>
  );
}
