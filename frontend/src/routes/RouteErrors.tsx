import { Button, Center, Container, Stack, Text, Title } from '@mantine/core';
import { IconCompass } from '@tabler/icons-react';
import { Link, isRouteErrorResponse, useRouteError } from 'react-router';

import { EmptyPanel } from '../components/EmptyPanel';
import { ErrorState } from '../components/ErrorState';

export function NotFoundPage() {
  return (
    <EmptyPanel
      icon={<IconCompass size={28} aria-hidden />}
      title="Page not found"
      description="The page you are looking for does not exist or has moved."
      action={
        <Button component={Link} to="/" variant="light">
          Back to overview
        </Button>
      }
    />
  );
}

export function RouteErrorBoundary() {
  const error = useRouteError();
  const isChunkError =
    error instanceof Error && /dynamically imported module|Importing a module script/i.test(error.message);
  return (
    <Center mih="60vh" p="md">
      <Container size="sm">
        <Stack gap="md">
          <Title order={2}>
            {isRouteErrorResponse(error) ? `${error.status} ${error.statusText}` : 'Unexpected error'}
          </Title>
          {isChunkError ? (
            <Text c="dimmed">A new version of NUVEXA was deployed. Reload the page to continue.</Text>
          ) : (
            <ErrorState error={error} />
          )}
          <Button onClick={() => window.location.reload()} w="fit-content">
            Reload page
          </Button>
        </Stack>
      </Container>
    </Center>
  );
}
