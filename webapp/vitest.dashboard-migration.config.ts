import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Focused migration boundary tests. The VM worker preloads networkGuard through
// the CLI. These tests mock the plugin connector, so no HTTP interceptor is needed.
export default defineConfig({
  plugins:[react()],
  test:{
    include:['src/screens/ExtensionWebApp.test.tsx'],
    environment:'jsdom',
    setupFiles:['./src/test/dashboardMigrationSetup.ts'],
    clearMocks:true,
    restoreMocks:true,
  },
});
