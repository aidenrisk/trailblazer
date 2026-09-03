// Constructs HumanBehavior on a FrameLocator, which crashes at runtime.
const fs = require('fs');
const path = require('path');

const METADATA = 'onboarding-pie-gl.metadata.json';
const QUESTIONS = 'onboarding-pie-gl.questions.json';

async function main() {
  const frame = page.frameLocator('#app-frame');
  const behavior = new HumanBehavior(frame);
  await behavior.type('#legalName', 'Acme LLC');
}

function report(status) {
  fs.writeFileSync(path.join(__dirname, 'last-run.json'), JSON.stringify(status));
  console.log('RRSTATUS ' + JSON.stringify(status));
}
