// dsh-cst-tools — DSH tool adapter for the CST_AI plugin (V2.0).
//
// Five tools are exposed.  They are a thin surface over the public Python API
// (`cst_ai_plugin.public`, PUBLIC_API_V1: plan / run / status / inspect / resume)
// and are launched through the PowerShell bridge `cst_ai_tool_bridge.ps1`, which
// in turn calls `cst_ai_tool.py`.
//
// The handlers below only build a request and hand it to the bridge.  They never
// connect to CST, never build, never solve and never extract by themselves; there
// is no second transport and no CLI subprocess.  Every tool returns the structured
// envelope the adapter produces, so a failure arrives as
// ok / error_type / stage / message / detail / run_id rather than as a bare
// thrown string.

import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const execFileAsync = promisify(execFile)

const POWERSHELL = 'powershell.exe'

const MODULE_DIR = dirname(fileURLToPath(import.meta.url))


// The bridge, the Python adapter and the Python package ship beside this module:
// the repository root is the DSH bundle, so an installed copy is self-contained.
// Set CST_AI_BRIDGE to an absolute path only to run against another installation.
function resolveCstAiBridge() {
  const fromEnv = process.env.CST_AI_BRIDGE

  if (
    typeof fromEnv === 'string' &&
    fromEnv.trim().length > 0
  ) {
    return fromEnv.trim()
  }

  return resolve(
    MODULE_DIR,
    'cst_ai_tool_bridge.ps1',
  )
}


function parseBridgeJson(stdout, label) {
  const text =
    typeof stdout === 'string'
      ? stdout.trim()
      : ''

  if (!text) {
    throw new Error(
      `${label} returned empty stdout.`,
    )
  }

  try {
    return JSON.parse(text)
  } catch {
    // cst.results may emit:
    //
    //   You are working in interactive mode.
    //   { ... JSON ... }
    //
    // Keep the bridge strict, but extract the actual JSON object
    // when harmless CST informational text precedes it.

    const firstBrace = text.indexOf('{')
    const lastBrace = text.lastIndexOf('}')

    if (
      firstBrace < 0 ||
      lastBrace < firstBrace
    ) {
      throw new Error(
        `${label} returned non-JSON stdout:\n${text}`,
      )
    }

    const jsonText =
      text.slice(
        firstBrace,
        lastBrace + 1,
      )

    try {
      return JSON.parse(jsonText)
    } catch {
      throw new Error(
        `${label} returned stdout whose JSON payload could not be parsed:\n${text}`,
      )
    }
  }
}


async function runPowerShellBridge(
  bridge,
  args,
  timeoutMs,
) {
  try {
    const result = await execFileAsync(
      POWERSHELL,
      [
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        bridge,
        ...args,
      ],
      {
        windowsHide: true,
        encoding: 'utf8',
        timeout: timeoutMs,
        maxBuffer: 4 * 1024 * 1024,
      },
    )

    const parsed =
      parseBridgeJson(
        result.stdout,
        bridge,
      )

    return JSON.stringify(
      parsed,
      null,
      2,
    )
  } catch (error) {
    const stdout =
      typeof error?.stdout === 'string'
        ? error.stdout.trim()
        : ''

    const stderr =
      typeof error?.stderr === 'string'
        ? error.stderr.trim()
        : ''

    let message =
      'CST bridge command failed.'

    if (stdout) {
      message +=
        `\nstdout:\n${stdout}`
    }

    if (stderr) {
      message +=
        `\nstderr:\n${stderr}`
    }

    if (
      !stdout &&
      !stderr &&
      error instanceof Error
    ) {
      message +=
        `\n${error.message}`
    }

    throw new Error(message)
  }
}


function textOutput() {
  return {
    schema: {
      type: 'string',
    },

    render(_args, value) {
      return [
        {
          type: 'text',
          text: value,
        },
      ]
    },
  }
}


export const name = 'cst-tools'
export const inject = ['tools']


export function apply(ctx) {

  // =====================================================================
  // CST_AI plugin tools.
  //
  // Thin surface over `cst_ai_plugin.public` (PUBLIC_API_V1: plan / run /
  // status / inspect / resume), launched through the CST_AI PowerShell
  // bridge.  PLAN never touches CST; STATUS and INSPECT read the run store
  // only; RUN and RESUME do whatever the ExecutionPlan authorises.
  // =====================================================================

  const CST_AI_BRIDGE = resolveCstAiBridge()

  function encodeCstAiRequest(request) {
    return Buffer.from(
      JSON.stringify(request),
      'utf8',
    ).toString('base64')
  }

  // a TaskSpec is accepted inline or as a path to one; nothing here accepts an
  // internal module or function name
  function applyTaskInput(request, args) {
    if (args?.task !== undefined) {
      request.task = args.task
    }

    if (args?.task_path !== undefined) {
      request.task_path = args.task_path
    }

    if (args?.store_root !== undefined) {
      request.store_root = args.store_root
    }

    return request
  }

  ctx.tools.register({
    name: 'plan_cst_ai_task',

    description:
      'Plan a structured CST_AI TaskSpec through the plugin orchestrator. ' +
      'ZERO SIDE EFFECT: does not connect to CST, does not build, does not solve and ' +
      'does not write any CST project. Returns task identity, stage order, whether a ' +
      'CST connection is required, planned solve count, the authorised solve budget, ' +
      'reuse decisions, expected outputs and blockers.',

    parameters: {
      type: 'object',

      properties: {
        task: {
          type: 'object',
          description: 'inline TaskSpec',
        },

        task_path: {
          type: 'string',
          description: 'path to a TaskSpec JSON file',
        },

        store_root: {
          type: 'string',
          description: 'optional run store root override',
        },
      },

      additionalProperties: false,
    },

    output: textOutput(),

    async execute(args) {
      return runPowerShellBridge(
        CST_AI_BRIDGE,
        [
          'plan',
          encodeCstAiRequest(applyTaskInput({}, args)),
        ],
        120000,
      )
    },
  })

  ctx.tools.register({
    name: 'run_cst_ai_task',

    description:
      'Execute a structured CST_AI TaskSpec through the plugin orchestrator and produce a ' +
      'durable ResultBundle. This is a thin adapter over the public API: the orchestrator ' +
      'performs the stages. Whether a solve may happen is decided solely by the TaskSpec ' +
      'solve_policy (FORBID / ALLOW_ONE / ALLOW_UP_TO_N / REUSE_ONLY); a solved artifact ' +
      'selected from the reuse cache is never re-solved. Large curves are returned as ' +
      'artifact ids, paths and hashes, not as raw sample arrays.',

    parameters: {
      type: 'object',

      properties: {
        task: {
          type: 'object',
          description: 'inline TaskSpec',
        },

        task_path: {
          type: 'string',
          description: 'path to a TaskSpec JSON file',
        },

        store_root: {
          type: 'string',
          description: 'optional run store root override',
        },

        reuse_index: {
          type: 'object',
          description: 'optional reuse cache: key -> solved artifact identity',
        },
      },

      additionalProperties: false,
    },

    output: textOutput(),

    async execute(args) {
      const request = applyTaskInput({}, args)

      if (args?.reuse_index !== undefined) {
        request.reuse_index = args.reuse_index
      }

      return runPowerShellBridge(
        CST_AI_BRIDGE,
        [
          'run',
          encodeCstAiRequest(request),
        ],
        1800000,
      )
    },
  })

  ctx.tools.register({
    name: 'get_cst_ai_run_status',

    description:
      'Read the durable status of a CST_AI run from the run store by run_id. ' +
      'Offline and read-only: zero CST contact, and it never opens a .cst project. ' +
      'Works across processes, so it is valid after the tool host restarts.',

    parameters: {
      type: 'object',

      properties: {
        run_id: {
          type: 'string',
        },

        store_root: {
          type: 'string',
          description: 'optional run store root override',
        },
      },

      required: [
        'run_id',
      ],

      additionalProperties: false,
    },

    output: textOutput(),

    async execute(args) {
      const request = {
        run_id: args?.run_id,
      }

      if (args?.store_root !== undefined) {
        request.store_root = args.store_root
      }

      return runPowerShellBridge(
        CST_AI_BRIDGE,
        [
          'status',
          encodeCstAiRequest(request),
        ],
        120000,
      )
    },
  })

  ctx.tools.register({
    name: 'inspect_cst_ai_run',

    description:
      'Read one section of a CST_AI run from the run store by run_id. ' +
      'Sections: summary, plan, artifacts, observables, errors, events, all. ' +
      'Offline and read-only: it never opens a .cst project to answer. Long arrays are ' +
      'reported by shape rather than by value.',

    parameters: {
      type: 'object',

      properties: {
        run_id: {
          type: 'string',
        },

        section: {
          type: 'string',

          enum: [
            'summary',
            'plan',
            'artifacts',
            'observables',
            'errors',
            'events',
            'all',
          ],
        },

        store_root: {
          type: 'string',
          description: 'optional run store root override',
        },
      },

      required: [
        'run_id',
      ],

      additionalProperties: false,
    },

    output: textOutput(),

    async execute(args) {
      const request = {
        run_id: args?.run_id,
        section:
          args?.section === undefined
            ? 'summary'
            : args.section,
      }

      if (args?.store_root !== undefined) {
        request.store_root = args.store_root
      }

      return runPowerShellBridge(
        CST_AI_BRIDGE,
        [
          'inspect',
          encodeCstAiRequest(request),
        ],
        120000,
      )
    },
  })

  ctx.tools.register({
    name: 'resume_cst_ai_run',

    description:
      'Resume an interrupted CST_AI run by run_id alone. The stored TaskSpec, execution ' +
      'plan, dependency snapshot, resolved-config snapshot and solve budget are recovered ' +
      'from the run store, so the full task does not have to be resubmitted. The solve ' +
      'budget is never reset and an already-complete run is a no-op. If the stored plan is ' +
      'incomplete and cannot be rebuilt to the same execution identity, the run is blocked ' +
      'rather than re-solved.',

    parameters: {
      type: 'object',

      properties: {
        run_id: {
          type: 'string',
        },

        task: {
          type: 'object',
          description:
            'optional TaskSpec, used only to integrity-check against the stored one; ' +
            'it never overwrites the stored task',
        },

        store_root: {
          type: 'string',
          description: 'optional run store root override',
        },
      },

      required: [
        'run_id',
      ],

      additionalProperties: false,
    },

    output: textOutput(),

    async execute(args) {
      const request = {
        run_id: args?.run_id,
      }

      if (args?.task !== undefined) {
        request.task = args.task
      }

      if (args?.store_root !== undefined) {
        request.store_root = args.store_root
      }

      return runPowerShellBridge(
        CST_AI_BRIDGE,
        [
          'resume',
          encodeCstAiRequest(request),
        ],
        1800000,
      )
    },
  })

}
