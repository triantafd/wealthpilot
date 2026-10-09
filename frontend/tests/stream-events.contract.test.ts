/**
 * The SSE contract, frontend side.
 *
 * Lives in `tests/` rather than under `src/features/`: it reads the filesystem
 * with node builtins, and `src` is typechecked by `tsconfig.app.json`, which
 * carries browser types only. `tsconfig.node.json` already declares `node`, so
 * this directory is included there instead of widening the app's types.
 *
 * Reads `shared/stream-events.ts` with the TypeScript compiler and compares the
 * `StreamEvent` union to `shared/stream-events.contract.json`, which the
 * backend generates from its Pydantic models. If either side gains, loses or
 * renames a field, this fails.
 *
 * The compiler rather than a regex: the union members are type literals with
 * nested references, and a regex that looked right would quietly stop matching
 * the first time someone reformatted the file.
 *
 * Only field names and a coarse kind are compared. Full type expressions are
 * spelled differently in each language, and a test needing a translation layer
 * per type is a test that gets disabled the first time it is wrong.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const SHARED = resolve(here, "../../shared");

type Kind = "string" | "number" | "boolean" | "array" | "object" | "unknown";
type Shape = Record<string, Record<string, { kind: Kind; required: boolean }>>;

const contract: { events: Shape } = JSON.parse(
  readFileSync(resolve(SHARED, "stream-events.contract.json"), "utf8"),
);

const sourcePath = resolve(SHARED, "stream-events.ts");
const source = ts.createSourceFile(
  sourcePath,
  readFileSync(sourcePath, "utf8"),
  ts.ScriptTarget.Latest,
  true,
);

/** Declarations in the file, so a type reference can be resolved to a kind. */
const interfaces = new Set<string>();
const stringAliases = new Set<string>();
let streamEvent: ts.TypeAliasDeclaration | undefined;

source.forEachChild((node) => {
  if (ts.isInterfaceDeclaration(node)) {
    interfaces.add(node.name.text);
  } else if (ts.isTypeAliasDeclaration(node)) {
    if (node.name.text === "StreamEvent") {
      streamEvent = node;
    } else if (
      ts.isUnionTypeNode(node.type) &&
      node.type.types.every((t) => ts.isLiteralTypeNode(t) && ts.isStringLiteral(t.literal))
    ) {
      // e.g. `type Tier = "READ" | "LOW" | "HIGH"` — a string on the wire.
      stringAliases.add(node.name.text);
    }
  }
});

function kindOf(node: ts.TypeNode): Kind {
  if (node.kind === ts.SyntaxKind.StringKeyword) return "string";
  if (node.kind === ts.SyntaxKind.NumberKeyword) return "number";
  if (node.kind === ts.SyntaxKind.BooleanKeyword) return "boolean";
  if (ts.isArrayTypeNode(node)) return "array";
  if (ts.isTypeLiteralNode(node)) return "object";
  if (ts.isLiteralTypeNode(node) && ts.isStringLiteral(node.literal)) return "string";
  if (ts.isTypeReferenceNode(node)) {
    const name = node.typeName.getText(source);
    if (name === "Record" || name === "Map") return "object";
    if (stringAliases.has(name)) return "string";
    if (interfaces.has(name)) return "object";
  }
  return "unknown";
}

/** The union, read back out of the TypeScript as the same shape the backend exports. */
function readStreamEvent(): Shape {
  if (!streamEvent) throw new Error("no StreamEvent type alias in shared/stream-events.ts");
  if (!ts.isUnionTypeNode(streamEvent.type)) {
    throw new Error("StreamEvent is expected to be a union of object literals");
  }

  const shape: Shape = {};
  for (const member of streamEvent.type.types) {
    if (!ts.isTypeLiteralNode(member)) throw new Error("union member is not an object literal");

    let discriminator: string | undefined;
    const fields: Record<string, { kind: Kind; required: boolean }> = {};

    for (const prop of member.members) {
      if (!ts.isPropertySignature(prop) || !prop.type) continue;
      const name = prop.name.getText(source);
      if (name === "type") {
        if (ts.isLiteralTypeNode(prop.type) && ts.isStringLiteral(prop.type.literal)) {
          discriminator = prop.type.literal.text;
        }
        continue;
      }
      fields[name] = { kind: kindOf(prop.type), required: prop.questionToken === undefined };
    }

    if (!discriminator) throw new Error("a union member has no string-literal `type`");
    shape[discriminator] = fields;
  }
  return shape;
}

describe("SSE contract", () => {
  const actual = readStreamEvent();

  it("declares the same events as the backend", () => {
    expect(Object.keys(actual).sort()).toEqual(Object.keys(contract.events).sort());
  });

  it("parsed a non-trivial union, so a silent parse failure cannot pass", () => {
    // Guards the test itself: an empty parse would make every comparison
    // vacuously true if the keys check above were ever loosened.
    expect(Object.keys(actual).length).toBeGreaterThanOrEqual(10);
    expect(actual.route).toBeDefined();
  });

  for (const [event, fields] of Object.entries(contract.events)) {
    it(`matches the backend on \`${event}\``, () => {
      expect(actual[event]).toEqual(fields);
    });
  }
});
