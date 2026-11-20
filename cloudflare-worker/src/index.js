import { reenableDisabledWorkflows } from "./keepalive.js";

export default {
  async scheduled(controller, env, _ctx) {
    const summary = await reenableDisabledWorkflows(env);
    console.log(
      JSON.stringify({
        event: "keepalive_complete",
        cron: controller.cron,
        scheduledTime: controller.scheduledTime,
        ...summary,
      }),
    );
  },
};
