// Where the page finds the backend's read-only API.
//  - Served by the backend itself (self-hosted, deploy/Caddyfile.snippet): "api", same origin.
//  - Served by Vercel (wow.osrs.dk, *.vercel.app previews): the backend's public API host.
//    That host needs this site's origin in FT_CORS_ORIGINS (deploy/forever-tracker.env.example).
const FT_VERCEL_HOSTS = ["wow.osrs.dk"];
const FT_PUBLIC_API = "https://wow-api.osrs.dk/api";

window.FT_CONFIG = {
  apiBase: FT_VERCEL_HOSTS.includes(location.hostname) || location.hostname.endsWith(".vercel.app")
    ? FT_PUBLIC_API
    : "api",
};
