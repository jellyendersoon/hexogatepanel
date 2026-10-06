/// <reference types="node" />
import { defineConfig } from 'orval'

export default defineConfig({
  app: {
    output: {
      client: 'react-query',
      target: './src/service/api/index.ts',
      mode: 'single',
      clean: false,
      prettier: true,
      tslint: true,
      headers: false,
      override: {
        mutator: {
          path: './src/service/http.ts',
          name: 'orvalFetcher',
        },
        fetch: {
          // The mutator returns the parsed body, so generate plain model types
          // instead of { data, status, headers } wrappers.
          includeHttpResponseReturnType: false,
        },
      },
    },
    input: {
      // Prefer a pre-extracted schema file (offline generation) when provided,
      // otherwise fall back to the running server's live endpoint.
      target: process.env.OPENAPI_INPUT || `http://127.0.0.1:${process.env.UVICORN_PORT || 8000}/openapi.json`,
    },
  },
})
