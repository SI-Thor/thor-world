# thor-world (working name)

The live wire between a Thor-generated world and the people and agents who use it:

- **world-mcp**: an MCP server over builder-ide's core, so any agent (Claude Code, zcode, mcode, others) can read the world's context and act on it through the typed vocabulary.
- **world-stream + viewer**: a deterministic recipe-to-glTF compiler with real LODs, GPU-compressed textures and a `since_version` delta channel, built for the Jetson AGX Thor, and a browser viewer on the ProArt that watches the world change live.

It fills the role of the private companion repo of the `maquette` Infinigen fork (an MCP server plus a browser viewer). It is our own design, optimised for the Thor; see builder-ide's `docs/research/maquette.md` for what we learned from that fork's public half. Licence: MIT.
