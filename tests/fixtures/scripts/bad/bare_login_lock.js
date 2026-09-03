// Binds acquireCarrierLoginLock() to a bare name and calls it. The call
// returns `{release}`, so invoking the object throws.
const fs = require('fs');
const path = require('path');

const METADATA = 'onboarding-pie-gl.metadata.json';
const QUESTIONS = 'onboarding-pie-gl.questions.json';

async function main() {
  const release = await acquireCarrierLoginLock();
  await login();
  release();
}

function report(status) {
  fs.writeFileSync(path.join(__dirname, 'last-run.json'), JSON.stringify(status));
  console.log('RRSTATUS ' + JSON.stringify(status));
}
