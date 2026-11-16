# Third-party notices

The offline API documentation page bundles these unmodified files from
`swagger-ui-dist` **5.34.1** under `backend/src/api_agent/static/`:

- `swagger-ui-bundle.js`
- `swagger-ui.css`
- `favicon-32x32.png`

The bundled files were checked against the exact npm distribution version.
Upstream project: [swagger-api/swagger-ui](https://github.com/swagger-api/swagger-ui).
Distribution: [swagger-ui-dist 5.34.1](https://www.npmjs.com/package/swagger-ui-dist/v/5.34.1).

Swagger UI is licensed under Apache License 2.0. The corresponding upstream
[`LICENSE`](backend/src/api_agent/static/LICENSE),
[`NOTICE`](backend/src/api_agent/static/NOTICE), and extracted bundle
[`third-party license notices`](backend/src/api_agent/static/swagger-ui-bundle.js.LICENSE.txt)
are included alongside the assets. These notices apply to the third-party
assets and do not assign a license to the API-Agent project itself.

Other Python and JavaScript dependencies are installed through the package
managers from `backend/uv.lock` and `frontend/package-lock.json`; their source
and installed dependency directories are not bundled in this repository.

The task-test page bundles three selected Lucide icons from Iconify's
`@iconify-json/lucide` package. The extracted path data is stored in
`frontend/src/lib/icons.generated.ts`, derived from the installed icon set;
Lucide and the data package are licensed under ISC. No icon runtime service or
full icon catalog is loaded by the browser.
