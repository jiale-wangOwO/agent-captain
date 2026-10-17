# Third-party notices

AgentCaptain's own source is covered by [LICENSE](LICENSE). Dependencies keep their own licenses and copyright notices; the project's MIT license does not replace them.

The source repository does not vendor `node_modules/`. `npm ci` installs the locked dependencies. If you distribute installed dependencies, browser assets, native binaries, or a packaged application, preserve the upstream license text and copyright notices with those copies.

## Primary terminal dependencies

| Component | Locked version | License and attribution |
|---|---|---|
| `node-pty` | 1.1.0 | MIT; its `LICENSE` includes Christopher Jeffrey (2012–2015), Daniel Imms (2016), and Microsoft Corporation (2018–present) |
| `@xterm/xterm` | 6.0.0 | MIT; The xterm.js authors (2017–2019), SourceLair Private Company (2014–2016), Christopher Jeffrey (2012–2013) |
| `@xterm/headless` | 6.0.0 | MIT, xterm.js project; retain the applicable upstream project notices |
| `@xterm/addon-fit` | 0.11.0 | MIT; The xterm.js authors (2019), as stated in its bundled `LICENSE` |
| `@xterm/addon-serialize` | 0.14.0 | Package declares MIT; retain the applicable xterm.js upstream notices when redistributing |
| `node-addon-api` | See `package-lock.json` | MIT; Node.js API collaborators (2017), as stated in `LICENSE.md` |
| winpty sources within `node-pty` | The dependency's bundled copy | MIT; Ryan Prichard (2011–2016), as stated in `deps/winpty/LICENSE` |

The installed `@xterm/addon-serialize` package inspected for this release declares MIT in `package.json` but does not include a standalone `LICENSE` file. Consult the [xterm.js upstream license](https://github.com/xtermjs/xterm.js/blob/master/LICENSE) when preparing a redistribution; absence of a separate package file is not permission to omit notices.

Upstream projects: [node-pty](https://github.com/microsoft/node-pty), [xterm.js](https://github.com/xtermjs/xterm.js), [node-addon-api](https://github.com/nodejs/node-addon-api), [winpty](https://github.com/rprichard/winpty).

This is a practical attribution summary, **not an exhaustive transitive-dependency license or security audit**. Check the installed lockfile, package licenses, and bundled source notices for the exact platform and artifact being distributed.

## External software and names

OpenSSH, Python, Node.js, tmux, Codex, and optional tunnel adapters are separate installations rather than copied project code. Their names and marks remain the property of their owners. AgentCaptain is not an official OpenAI product and does not imply endorsement by any listed project.
