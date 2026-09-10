'use strict';
// Preserve links saved before the workspace received its own route.
if (/^#(?:monitors|history|notifications|connections|connect|monitor\/[a-f0-9]{32})$/.test(location.hash)) {
  location.replace('/workspace' + location.hash);
}
