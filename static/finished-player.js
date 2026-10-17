'use strict';
// Compatibility for saved delivery links; all playback belongs to the normal room.
location.replace('/watch?' + new URLSearchParams(location.search).toString());
