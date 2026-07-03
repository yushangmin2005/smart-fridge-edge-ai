import { execFile as execFileCallback } from "node:child_process";
import { mkdir, readdir, readlink, stat } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join, resolve } from "node:path";
import { promisify } from "node:util";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const execFile = promisify(execFileCallback);

type CommandResult = {
	ok: boolean;
	command: string;
	args: string[];
	stdout: string;
	stderr: string;
	error?: string;
	code?: number | string;
};

type DeviceInfo = {
	path: string;
	target?: string;
	mode?: string;
	uid?: number;
	gid?: number;
	error?: string;
};

function modeString(mode: number): string {
	return "0" + (mode & 0o777).toString(8);
}

async function run(command: string, args: string[] = [], timeoutMs = 8000): Promise<CommandResult> {
	try {
		const result = await execFile(command, args, { timeout: timeoutMs, maxBuffer: 1024 * 1024 });
		return {
			ok: true,
			command,
			args,
			stdout: String(result.stdout || "").trim(),
			stderr: String(result.stderr || "").trim(),
		};
	} catch (error: any) {
		return {
			ok: false,
			command,
			args,
			stdout: String(error?.stdout || "").trim(),
			stderr: String(error?.stderr || "").trim(),
			error: error?.message || String(error),
			code: error?.code,
		};
	}
}

async function commandPath(command: string): Promise<string | null> {
	const result = await run("bash", ["-lc", `command -v ${command}`], 3000);
	return result.ok && result.stdout ? result.stdout.split("\n")[0] : null;
}

async function listDeviceFiles(pattern: RegExp): Promise<DeviceInfo[]> {
	const entries = await readdir("/dev");
	const paths = entries
		.filter((name) => pattern.test(name))
		.sort((left, right) => left.localeCompare(right, undefined, { numeric: true }))
		.map((name) => join("/dev", name));
	return Promise.all(paths.map(deviceInfo));
}

async function deviceInfo(path: string): Promise<DeviceInfo> {
	try {
		const info = await stat(path);
		const result: DeviceInfo = {
			path,
			mode: modeString(info.mode),
			uid: info.uid,
			gid: info.gid,
		};
		try {
			result.target = await readlink(path);
		} catch {
			// Not a symlink.
		}
		return result;
	} catch (error: any) {
		return { path, error: error?.message || String(error) };
	}
}

function normalizeDevice(input: string, prefix: string, pattern: RegExp): string {
	const value = String(input || "").trim();
	const path = value.startsWith("/dev/")
		? value
		: `/dev/${value.startsWith(prefix) || value.startsWith(`${prefix}-`) ? value : `${prefix}${value}`}`;
	const resolved = resolve(path);
	if (!pattern.test(resolved)) {
		throw new Error(`Unsupported device path: ${input}`);
	}
	return resolved;
}

function isAllowedOutputDir(outputDir: string): boolean {
	const home = process.env.HOME ? resolve(process.env.HOME) : "";
	return outputDir === "/tmp" || outputDir.startsWith("/tmp/") || (!!home && (outputDir === home || outputDir.startsWith(`${home}/`)));
}

function textResult(title: string, payload: unknown) {
	return {
		content: [{ type: "text" as const, text: `${title}\n${JSON.stringify(payload, null, 2)}` }],
		details: payload,
	};
}

async function inventoryPayload() {
	const [uname, id, gpiochips, i2c, spi, serial, video, tools, v4l2] = await Promise.all([
		run("uname", ["-a"]),
		run("id", []),
		listDeviceFiles(/^gpiochip\d+$/),
		listDeviceFiles(/^i2c-\d+$/),
		listDeviceFiles(/^spidev.+$/),
		listDeviceFiles(/^(ttyS\d+|ttyUSB\d+|ttyACM\d+)$/),
		listDeviceFiles(/^video.*$/),
		Promise.all(
			["gpioinfo", "gpioget", "gpioset", "i2cdetect", "i2cget", "i2cset", "ffmpeg", "v4l2-ctl"].map(async (name) => [
				name,
				await commandPath(name),
			]),
		),
		run("bash", ["-lc", "command -v v4l2-ctl >/dev/null 2>&1 && v4l2-ctl --list-devices || true"], 8000),
	]);
	return {
		host: process.env.HOSTNAME || "NanoPC-T4",
		uname: uname.stdout,
		id: id.stdout,
		devices: { gpiochips, i2c, spi, serial, video },
		tools: Object.fromEntries(tools),
		v4l2: v4l2.stdout,
		notes: [
			"GPIO write is disabled unless SMART_FRIDGE_PI_TOOLS_ALLOW_GPIO_WRITE=1 is set.",
			"I2C scan/read/write require i2c-tools and device permissions.",
			"On this board /dev/gpiochip* is often root-only; read/write tools report permission errors instead of escalating.",
		],
	};
}

const boardInventoryTool = defineTool({
	name: "board_inventory",
	label: "Board Inventory",
	description: "List NanoPC-T4 GPIO chips and peripheral device nodes visible to the Pi agent.",
	promptSnippet: "Inspect the board GPIO/peripheral inventory before touching hardware.",
	promptGuidelines: [
		"Use board_inventory before GPIO/I2C/camera operations to understand which devices and command-line tools are available.",
	],
	parameters: Type.Object({}),
	async execute() {
		return textResult("Board inventory", await inventoryPayload());
	},
});

const boardGpioInfoTool = defineTool({
	name: "board_gpio_info",
	label: "GPIO Info",
	description: "Show GPIO chip inventory and, when gpioinfo is installed, GPIO line information.",
	promptSnippet: "Inspect GPIO chips and line metadata.",
	parameters: Type.Object({
		chip: Type.Optional(Type.String({ description: "Optional chip such as gpiochip0 or /dev/gpiochip0." })),
	}),
	async execute(_id, params) {
		const gpioinfo = await commandPath("gpioinfo");
		const gpiochips = await listDeviceFiles(/^gpiochip\d+$/);
		if (!gpioinfo) {
			return textResult("GPIO info", {
				ok: false,
				reason: "gpioinfo_not_installed",
				gpiochips,
				note: "Install libgpiod tools and grant device permissions before GPIO line inspection is possible.",
			});
		}
		const args = params.chip ? [normalizeDevice(params.chip, "gpiochip", /^\/dev\/gpiochip\d+$/)] : [];
		const result = await run(gpioinfo, args, 10000);
		return textResult("GPIO info", { gpiochips, gpioinfo: result });
	},
});

const boardGpioReadTool = defineTool({
	name: "board_gpio_read",
	label: "GPIO Read",
	description: "Read one GPIO line using gpioget. Does not escalate privileges.",
	parameters: Type.Object({
		chip: Type.String({ description: "GPIO chip, for example gpiochip0 or /dev/gpiochip0." }),
		line: Type.Integer({ minimum: 0, description: "GPIO line offset on the chip." }),
	}),
	async execute(_id, params) {
		const gpioget = await commandPath("gpioget");
		if (!gpioget) {
			return textResult("GPIO read", { ok: false, reason: "gpioget_not_installed" });
		}
		const chip = normalizeDevice(params.chip, "gpiochip", /^\/dev\/gpiochip\d+$/);
		const result = await run(gpioget, [chip, String(params.line)], 5000);
		return textResult("GPIO read", { chip, line: params.line, result });
	},
});

const boardGpioWriteTool = defineTool({
	name: "board_gpio_write",
	label: "GPIO Write",
	description: "Set one GPIO line using gpioset. Guarded by environment and explicit confirmation.",
	parameters: Type.Object({
		chip: Type.String({ description: "GPIO chip, for example gpiochip0 or /dev/gpiochip0." }),
		line: Type.Integer({ minimum: 0, description: "GPIO line offset on the chip." }),
		value: Type.Integer({ minimum: 0, maximum: 1, description: "Output value: 0 or 1." }),
		confirm: Type.String({ description: "Must equal: I understand this may change hardware state" }),
	}),
	async execute(_id, params) {
		if (process.env.SMART_FRIDGE_PI_TOOLS_ALLOW_GPIO_WRITE !== "1") {
			return textResult("GPIO write", {
				ok: false,
				reason: "gpio_write_disabled",
				enable: "Set SMART_FRIDGE_PI_TOOLS_ALLOW_GPIO_WRITE=1 for the Pi process after verifying pin mapping and connected hardware.",
			});
		}
		if (params.confirm !== "I understand this may change hardware state") {
			return textResult("GPIO write", { ok: false, reason: "confirmation_required" });
		}
		const gpioset = await commandPath("gpioset");
		if (!gpioset) {
			return textResult("GPIO write", { ok: false, reason: "gpioset_not_installed" });
		}
		const chip = normalizeDevice(params.chip, "gpiochip", /^\/dev\/gpiochip\d+$/);
		const result = await run(gpioset, [chip, `${params.line}=${params.value}`], 5000);
		return textResult("GPIO write", { chip, line: params.line, value: params.value, result });
	},
});

const boardI2cScanTool = defineTool({
	name: "board_i2c_scan",
	label: "I2C Scan",
	description: "Scan one I2C bus with i2cdetect when available.",
	parameters: Type.Object({
		bus: Type.Integer({ minimum: 0, description: "I2C bus number, for example 0, 1, 2, 4, 7, 9, or 10." }),
	}),
	async execute(_id, params) {
		const buses = await listDeviceFiles(/^i2c-\d+$/);
		const i2cdetect = await commandPath("i2cdetect");
		if (!i2cdetect) {
			return textResult("I2C scan", { ok: false, reason: "i2cdetect_not_installed", buses });
		}
		const device = `/dev/i2c-${params.bus}`;
		if (!existsSync(device)) {
			return textResult("I2C scan", { ok: false, reason: "i2c_bus_not_found", bus: params.bus, buses });
		}
		const result = await run(i2cdetect, ["-y", String(params.bus)], 12000);
		return textResult("I2C scan", { bus: params.bus, device, result });
	},
});

const boardI2cReadTool = defineTool({
	name: "board_i2c_read",
	label: "I2C Read",
	description: "Read one register from an I2C device with i2cget when available.",
	parameters: Type.Object({
		bus: Type.Integer({ minimum: 0, description: "I2C bus number." }),
		address: Type.String({ description: "I2C device address, for example 0x40." }),
		register: Type.String({ description: "Register address, for example 0x00." }),
		mode: Type.Optional(Type.String({ description: "i2cget mode such as b, w, or c. Defaults to b." })),
	}),
	async execute(_id, params) {
		const buses = await listDeviceFiles(/^i2c-\d+$/);
		const i2cget = await commandPath("i2cget");
		if (!i2cget) {
			return textResult("I2C read", { ok: false, reason: "i2cget_not_installed", buses });
		}
		const device = `/dev/i2c-${params.bus}`;
		if (!existsSync(device)) {
			return textResult("I2C read", { ok: false, reason: "i2c_bus_not_found", bus: params.bus, buses });
		}
		const mode = params.mode || "b";
		const result = await run(i2cget, ["-y", String(params.bus), params.address, params.register, mode], 5000);
		return textResult("I2C read", { bus: params.bus, device, address: params.address, register: params.register, mode, result });
	},
});

const boardI2cWriteTool = defineTool({
	name: "board_i2c_write",
	label: "I2C Write",
	description: "Write one register on an I2C device with i2cset. Guarded by environment and explicit confirmation.",
	parameters: Type.Object({
		bus: Type.Integer({ minimum: 0, description: "I2C bus number." }),
		address: Type.String({ description: "I2C device address, for example 0x40." }),
		register: Type.String({ description: "Register address, for example 0x00." }),
		value: Type.String({ description: "Value to write, for example 0x01." }),
		mode: Type.Optional(Type.String({ description: "i2cset mode such as b or w. Defaults to b." })),
		confirm: Type.String({ description: "Must equal: I understand this may change hardware state" }),
	}),
	async execute(_id, params) {
		if (process.env.SMART_FRIDGE_PI_TOOLS_ALLOW_I2C_WRITE !== "1") {
			return textResult("I2C write", {
				ok: false,
				reason: "i2c_write_disabled",
				enable: "Set SMART_FRIDGE_PI_TOOLS_ALLOW_I2C_WRITE=1 for the Pi process after verifying bus, address, register, and connected hardware.",
			});
		}
		if (params.confirm !== "I understand this may change hardware state") {
			return textResult("I2C write", { ok: false, reason: "confirmation_required" });
		}
		const buses = await listDeviceFiles(/^i2c-\d+$/);
		const i2cset = await commandPath("i2cset");
		if (!i2cset) {
			return textResult("I2C write", { ok: false, reason: "i2cset_not_installed", buses });
		}
		const device = `/dev/i2c-${params.bus}`;
		if (!existsSync(device)) {
			return textResult("I2C write", { ok: false, reason: "i2c_bus_not_found", bus: params.bus, buses });
		}
		const mode = params.mode || "b";
		const result = await run(i2cset, ["-y", String(params.bus), params.address, params.register, params.value, mode], 5000);
		return textResult("I2C write", { bus: params.bus, device, address: params.address, register: params.register, value: params.value, mode, result });
	},
});

const boardCameraCaptureTool = defineTool({
	name: "board_camera_capture",
	label: "Camera Capture",
	description: "Capture one JPEG frame from a video device with ffmpeg and return the output path.",
	parameters: Type.Object({
		device: Type.Optional(Type.String({ description: "Video device. Defaults to /dev/video10." })),
		outputDir: Type.Optional(Type.String({ description: "Output directory. Defaults to /tmp/pi-board-tools/captures." })),
		width: Type.Optional(Type.Integer({ minimum: 1, default: 640 })),
		height: Type.Optional(Type.Integer({ minimum: 1, default: 360 })),
	}),
	async execute(_id, params) {
		const ffmpeg = await commandPath("ffmpeg");
		if (!ffmpeg) {
			return textResult("Camera capture", { ok: false, reason: "ffmpeg_not_installed" });
		}
		const device = normalizeDevice(params.device || "/dev/video10", "video", /^\/dev\/video(?:-camera0|\d+)$/);
		const outputDir = resolve(params.outputDir || "/tmp/pi-board-tools/captures");
		if (!isAllowedOutputDir(outputDir)) {
			return textResult("Camera capture", { ok: false, reason: "output_dir_not_allowed", outputDir });
		}
		await mkdir(outputDir, { recursive: true });
		const width = params.width || 640;
		const height = params.height || 360;
		const output = join(outputDir, `capture-${new Date().toISOString().replace(/[:.]/g, "")}.jpg`);
		const result = await run(
			ffmpeg,
			[
				"-hide_banner",
				"-loglevel",
				"error",
				"-y",
				"-f",
				"v4l2",
				"-video_size",
				`${width}x${height}`,
				"-i",
				device,
				"-frames:v",
				"1",
				output,
			],
			30000,
		);
		return textResult("Camera capture", { device, output, width, height, result, file: await deviceInfo(output) });
	},
});

export default function (pi: ExtensionAPI) {
	pi.registerTool(boardInventoryTool);
	pi.registerTool(boardGpioInfoTool);
	pi.registerTool(boardGpioReadTool);
	pi.registerTool(boardGpioWriteTool);
	pi.registerTool(boardI2cScanTool);
	pi.registerTool(boardI2cReadTool);
	pi.registerTool(boardI2cWriteTool);
	pi.registerTool(boardCameraCaptureTool);
}
