import { parse } from "@babel/parser"

const identifier = (node, name) => node?.type === "Identifier" && node.name === name
const memberExpression = node => node?.type === "MemberExpression" || node?.type === "OptionalMemberExpression"
const callExpression = node => node?.type === "CallExpression" || node?.type === "OptionalCallExpression"

function isImportMeta(node) {
  return node?.type === "MetaProperty" && identifier(node.meta, "import") && identifier(node.property, "meta")
}

function memberName(node) {
  if (!memberExpression(node)) return null
  return node.computed
    ? node.property?.type === "StringLiteral" ? node.property.value : null
    : node.property?.name ?? null
}

function isImportMetaUrl(node) {
  return memberExpression(node) && isImportMeta(node.object) && memberName(node) === "url"
}

function isBoundInScope(name, scope) {
  for (let current = scope; current; current = current.parent) {
    if (current.bindings.has(name)) return true
  }
  return false
}

function isGlobalObject(node, scope) {
  return ["globalThis", "window", "self", "global"].some(
    name => identifier(node, name) && !isBoundInScope(name, scope),
  )
}

function isGlobalSelfReference(node, scope) {
  return memberExpression(node) && isGlobalObject(node.object, scope) &&
    ["globalThis", "global", "self", "window"].includes(memberName(node))
}

function isLoaderMember(node, scope) {
  if (!memberExpression(node)) return false
  const name = memberName(node)
  if (identifier(node.object, "require")) return true
  if (identifier(node.object, "module")) return name === "require" || node.computed && name === null
  if (isImportMeta(node.object)) return name === "resolve" || node.computed && name === null
  if (isGlobalObject(node.object, scope)) return ["require", "importScripts"].includes(name)
  return false
}

function isModuleLoaderExpression(node, scope) {
  return identifier(node, "require") || identifier(node, "importScripts") || isLoaderMember(node, scope)
}

function moduleCallName(callee, scope) {
  if (callee?.type === "Import") return "import()"
  if (identifier(callee, "require") || identifier(callee, "importScripts")) return `${callee.name}()`
  if (identifier(callee, "createRequire")) return "createRequire()"
  if (!memberExpression(callee)) return null

  const propertyName = memberName(callee)
  const dynamicProperty = propertyName === null && callee.computed
  if (["call", "apply", "bind"].includes(propertyName) && isModuleLoaderExpression(callee.object, scope)) {
    return "indirect-loader()"
  }
  if (identifier(callee.object, "require")) {
    return propertyName === "resolve" ? "require.resolve()" : "unsupported-loader-member()"
  }
  if (identifier(callee.object, "module")) {
    return propertyName === "require" ? "module.require()" : dynamicProperty ? "unsupported-loader-member()" : null
  }
  if (isImportMeta(callee.object)) {
    return propertyName === "resolve" ? "import.meta.resolve()" : dynamicProperty ? "unsupported-loader-member()" : null
  }
  if (isGlobalObject(callee.object, scope)) {
    if (propertyName === "require") return `${callee.object.name}.require()`
    if (propertyName === "importScripts") return `${callee.object.name}.importScripts()`
    if (["eval", "Function", "createRequire"].includes(propertyName)) return "dynamic-code()"
    if (dynamicProperty) return "unsupported-loader-member()"
  }
  return null
}

function isUrlConstructor(node, scope) {
  return identifier(node, "URL") && !isBoundInScope("URL", scope) ||
    memberExpression(node) && isGlobalObject(node.object, scope) && memberName(node) === "URL"
}

function containsImportMetaUrl(node) {
  if (!node || typeof node !== "object") return false
  if (isImportMetaUrl(node)) return true
  for (const [key, value] of Object.entries(node)) {
    if (["loc", "tokens", "comments", "leadingComments", "innerComments", "trailingComments"].includes(key)) continue
    if (Array.isArray(value)) {
      if (value.some(child => child && typeof child === "object" && containsImportMetaUrl(child))) return true
    } else if (value && typeof value === "object" && containsImportMetaUrl(value)) {
      return true
    }
  }
  return false
}

function isTypeOnlyParent(parent) {
  return ["TSTypeReference", "TSTypeQuery", "TSQualifiedName", "TSExpressionWithTypeArguments"].includes(parent?.type)
}

function isHarmlessPropertyKey(node, parent) {
  if (!parent || parent.shorthand) return false
  if ((parent.type === "ObjectProperty" || parent.type === "ObjectMethod" || parent.type === "ClassMethod" ||
      parent.type === "TSPropertySignature" || parent.type === "TSMethodSignature" || parent.type === "ClassProperty" ||
      parent.type === "ClassPrivateProperty") &&
      parent.key === node && !parent.computed) return true
  return memberExpression(parent) && parent.property === node && !parent.computed
}

function addPatternBindings(scope, pattern) {
  if (!scope || !pattern || typeof pattern !== "object") return
  if (pattern.type === "Identifier") {
    scope.bindings.add(pattern.name)
  } else if (pattern.type === "RestElement" || pattern.type === "TSParameterProperty") {
    addPatternBindings(scope, pattern.argument ?? pattern.parameter)
  } else if (pattern.type === "AssignmentPattern") {
    addPatternBindings(scope, pattern.left)
  } else if (pattern.type === "ArrayPattern") {
    for (const element of pattern.elements) addPatternBindings(scope, element)
  } else if (pattern.type === "ObjectPattern") {
    for (const property of pattern.properties) {
      addPatternBindings(scope, property.type === "RestElement" ? property.argument : property.value)
    }
  }
}

function nearestFunctionScope(scope) {
  let current = scope
  while (current && current.kind !== "function") current = current.parent
  return current
}

function buildScopes(ast) {
  const scopeForNode = new WeakMap()

  function visit(node, parentScope = null, parentNode = null) {
    if (!node || typeof node !== "object") return
    let scope = parentScope
    let functionBodyScope = null

    if (node.type === "Program") {
      scope = { kind: "function", parent: parentScope, bindings: new Set() }
    } else if (["FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression", "ObjectMethod", "ClassMethod",
      "ClassPrivateMethod"].includes(node.type)) {
      scope = { kind: "function", parent: parentScope, bindings: new Set() }
      functionBodyScope = { kind: "function", parent: scope, bindings: new Set() }
      if (node.type === "FunctionExpression" && node.id) addPatternBindings(scope, node.id)
      for (const parameter of node.params ?? []) addPatternBindings(scope, parameter)
    } else if (node.type === "StaticBlock") {
      scope = { kind: "function", parent: parentScope, bindings: new Set() }
    } else if (node.type === "ClassExpression" || node.type === "ClassDeclaration") {
      scope = { kind: "block", parent: parentScope, bindings: new Set() }
      if (node.id) addPatternBindings(scope, node.id)
    } else if (["BlockStatement", "CatchClause", "ForStatement", "ForInStatement", "ForOfStatement", "SwitchStatement"].includes(node.type)) {
      scope = { kind: "block", parent: parentScope, bindings: new Set() }
      if (node.type === "CatchClause") addPatternBindings(scope, node.param)
    }
    scopeForNode.set(node, scope)

    if (node.type === "FunctionDeclaration" && node.id) addPatternBindings(parentScope, node.id)
    if (node.type === "ClassDeclaration" && node.id) addPatternBindings(parentScope, node.id)
    if (node.type === "VariableDeclarator") {
      const destination = parentNode?.kind === "var" ? nearestFunctionScope(scope) : scope
      addPatternBindings(destination, node.id)
    }
    if (node.type === "ImportDeclaration") {
      for (const specifier of node.specifiers) addPatternBindings(scope, specifier.local)
    }
    if (node.type === "TSImportEqualsDeclaration") addPatternBindings(scope, node.id)

    for (const [key, value] of Object.entries(node)) {
      if (["loc", "tokens", "comments", "leadingComments", "innerComments", "trailingComments"].includes(key)) continue
      let childScope = functionBodyScope && key === "body" ? functionBodyScope : scope
      if (node.computed && key === "key") childScope = parentScope
      if (node.type === "SwitchStatement" && key === "discriminant") childScope = parentScope
      if (Array.isArray(value)) {
        for (const child of value) if (child && typeof child === "object" && typeof child.type === "string") visit(child, childScope, node)
      } else if (value && typeof value === "object" && typeof value.type === "string") {
        visit(value, childScope, node)
      }
    }
  }

  visit(ast.program)
  return scopeForNode
}

function directCallOf(node, parent) {
  return callExpression(parent) && parent.callee === node
}

function isAllowedGlobalObjectUse(node, parent, scope) {
  if (memberExpression(parent) && parent.object === node) return true
  if (callExpression(parent) && memberExpression(parent.callee) && memberName(parent.callee) === "bind" &&
      parent.arguments?.length === 1 && parent.arguments[0] === node &&
      memberExpression(parent.callee.object) && memberName(parent.callee.object) === "fetch" &&
      isGlobalObject(parent.callee.object.object, scope)) return true
  return false
}

function directUrlBase(node, parent, scope) {
  return parent?.type === "NewExpression" && isUrlConstructor(parent.callee, scope) && parent.arguments?.[1] === node
}

function directFileUrlConversion(node, parent) {
  return callExpression(parent) && identifier(parent.callee, "fileURLToPath") && parent.arguments?.[0] === node
}

function isConstructorProperty(node) {
  return memberExpression(node) && memberName(node) === "constructor"
}

export function collectModuleSpecifiers(source, fileName = "source.js") {
  const declarationFile = fileName.endsWith(".d.ts")
  const ast = parse(source, {
    sourceType: "unambiguous",
    allowAwaitOutsideFunction: true,
    plugins: [["typescript", { dts: declarationFile }], "jsx", "importAttributes"],
  })
  const scopeForNode = buildScopes(ast)
  const specifiers = new Map()

  function addSpecifier(node, context, resolveAgainstImporter = false) {
    if (node?.type !== "StringLiteral" || node.value.length === 0) {
      throw new Error(`Non-literal or empty module specifier in ${context}`)
    }
    if (node.value === "node:module") {
      throw new Error("The node:module API is unsupported in isolated SDK source")
    }
    if (node.value.includes("\\")) {
      throw new Error(`Escaped module specifier is not supported in ${context}: ${node.value}`)
    }
    specifiers.set(`${resolveAgainstImporter ? "url" : "module"}:${node.value}`, {
      specifier: node.value,
      resolveAgainstImporter,
    })
  }

  function visit(node, parent = null, grandparent = null) {
    if (!node || typeof node !== "object") return
    const scope = scopeForNode.get(node)

    if (node.type === "Identifier" && !isBoundInScope(node.name, scope)) {
      if (["eval", "Function"].includes(node.name) && !isTypeOnlyParent(parent) &&
          !isHarmlessPropertyKey(node, parent)) {
        throw new Error(`Dynamic code execution via ${node.name} is unsupported in isolated SDK source`)
      }
      if (node.name === "createRequire" && !isHarmlessPropertyKey(node, parent)) {
        throw new Error("createRequire() is unsupported in isolated SDK source")
      }
      if (["require", "importScripts"].includes(node.name) &&
          !(directCallOf(node, parent) || memberExpression(parent) && parent.object === node && directCallOf(parent, grandparent)) &&
          !isHarmlessPropertyKey(node, parent)) {
        throw new Error(`Module loader ${node.name} cannot be aliased or passed as a value`)
      }
      if (node.name === "module" &&
          !(memberExpression(parent) && parent.object === node && memberName(parent) === "require" &&
            directCallOf(parent, grandparent)) && !isHarmlessPropertyKey(node, parent)) {
        throw new Error("The CommonJS module object cannot be aliased in isolated SDK source")
      }
      if (["globalThis", "window", "self", "global"].includes(node.name) &&
          !isAllowedGlobalObjectUse(node, parent, scope) && !isHarmlessPropertyKey(node, parent)) {
        throw new Error(`Global object ${node.name} cannot be aliased in isolated SDK source`)
      }
    }

    if (node.type === "MetaProperty" && isImportMeta(node) && !memberExpression(parent)) {
      throw new Error("import.meta cannot be aliased in isolated SDK source")
    }

    if (memberExpression(node)) {
      const name = memberName(node)
      if (isGlobalObject(node.object, scope) && ["eval", "Function", "createRequire"].includes(name)) {
        throw new Error(`Dynamic code or module loading via ${name} is unsupported in isolated SDK source`)
      }
      if (isConstructorProperty(node)) {
        throw new Error("Constructor property access is unsupported in isolated SDK source")
      }
      if (isGlobalSelfReference(node, scope)) {
        throw new Error("Global self-reference is unsupported in isolated SDK source")
      }
      if (isGlobalObject(node.object, scope) && node.computed && name === null) {
        throw new Error("Computed global object properties are unsupported in isolated SDK source")
      }
      if (isImportMetaUrl(node) && !directUrlBase(node, parent, scope) && !directFileUrlConversion(node, parent)) {
        throw new Error("Aliasing import.meta.url is unsupported in isolated SDK source")
      }
      if (isLoaderMember(node, scope) && !directCallOf(node, parent)) {
        throw new Error("Module loader references cannot be aliased or passed as values")
      }
    }

    if (node.type === "ImportDeclaration" || node.type === "ExportNamedDeclaration" || node.type === "ExportAllDeclaration") {
      if (node.source) addSpecifier(node.source, "static import/export")
    } else if (node.type === "TSImportEqualsDeclaration" && node.moduleReference?.type === "TSExternalModuleReference") {
      addSpecifier(node.moduleReference.expression, "TypeScript import-equals")
    } else if (node.type === "TSImportType") {
      addSpecifier(node.argument, "TypeScript import type")
    } else if (callExpression(node)) {
      if (identifier(node.callee, "eval") && !isBoundInScope("eval", scope)) {
        throw new Error("Dynamic code evaluation with eval() is unsupported in isolated SDK source")
      }
      const callName = moduleCallName(node.callee, scope)
      if (callName) {
        if (callName === "createRequire()" || callName === "dynamic-code()") {
          throw new Error("Dynamic code or module loading via unsupported API")
        }
        if (callName === "indirect-loader()" || callName === "unsupported-loader-member()") {
          throw new Error(`Indirect or computed module loader is unsupported: ${callName}`)
        }
        if (node.arguments.length === 0) throw new Error(`Missing module specifier in ${callName}`)
        if (callName.endsWith(".require()") || callName === "require()" ||
            callName === "require.resolve()" || callName === "import.meta.resolve()") {
          if (node.arguments.length !== 1) throw new Error(`Unexpected resolver options in ${callName}`)
        }
        const args = callName.endsWith("importScripts()") ? node.arguments : [node.arguments[0]]
        for (const argument of args) {
          if (argument?.type === "SpreadElement") throw new Error(`Non-literal module specifier in ${callName}`)
          addSpecifier(argument, callName)
        }
      }
    } else if (node.type === "ImportExpression") {
      addSpecifier(node.source, "import()")
    } else if (node.type === "NewExpression") {
      if (node.arguments?.length >= 2) {
        if (isImportMetaUrl(node.arguments[1])) {
          if (!isUrlConstructor(node.callee, scope)) {
            throw new Error("Only the URL constructor may resolve paths against import.meta.url")
          }
          addSpecifier(node.arguments[0], "new URL(..., import.meta.url)", true)
        } else if (memberExpression(node.arguments[1]) && isImportMeta(node.arguments[1].object) &&
            node.arguments[1].computed && memberName(node.arguments[1]) === null) {
          throw new Error("Computed import.meta URL properties are unsupported in isolated SDK source")
        } else if (containsImportMetaUrl(node.arguments[1])) {
          throw new Error("Complex importer-relative URL bases are unsupported in isolated SDK source")
        }
      }
      const callName = moduleCallName(node.callee, scope)
      if (callName === "dynamic-code()" || callName === "createRequire()") {
        throw new Error("Dynamic code or module loading via unsupported API")
      }
    }

    for (const [key, value] of Object.entries(node)) {
      if (["loc", "tokens", "comments", "leadingComments", "innerComments", "trailingComments"].includes(key)) continue
      if (Array.isArray(value)) {
        for (const child of value) if (child && typeof child === "object" && typeof child.type === "string") visit(child, node, parent)
      } else if (value && typeof value === "object" && typeof value.type === "string") {
        visit(value, node, parent)
      }
    }
  }

  visit(ast.program)
  return [...specifiers.values()]
}
