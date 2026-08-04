import { defineConfig } from 'astro/config';
import tailwindcss from '@tailwindcss/vite';
import react from '@astrojs/react';
import sitemap from '@astrojs/sitemap';
import vercel from '@astrojs/vercel';

// https://astro.build/config
export default defineConfig({
  site: 'https://www.venue-economics.com',
  trailingSlash: 'never',
  // `output` stays unset => 'static' default: the homepage is prerendered.
  // Only the `/api/predict` route opts into on-demand rendering via
  // `export const prerender = false`. Do NOT set output:'server' — that would
  // turn the whole site into a function and change Vercel's build/cost model.
  adapter: vercel(),
  vite: {
    plugins: [tailwindcss()],
  },
  integrations: [react(), sitemap()],
});
