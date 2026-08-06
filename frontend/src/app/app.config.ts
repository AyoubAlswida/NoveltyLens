import { ApplicationConfig, provideBrowserGlobalErrorListeners } from '@angular/core';

// No router: the app has always switched screens from a single state value and
// has no URLs to restore. Adding routing would be new behaviour, not a port.
export const appConfig: ApplicationConfig = {
  providers: [provideBrowserGlobalErrorListeners()],
};
