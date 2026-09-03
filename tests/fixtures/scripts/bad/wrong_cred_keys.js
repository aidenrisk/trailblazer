// Reads credentials under keys the creds file does not carry. The reads yield
// undefined, an empty password still gets typed, and the portal reports bad
// credentials rather than a bug.
const fs = require('fs');
const path = require('path');

const METADATA = 'onboarding-pie-gl.metadata.json';
const QUESTIONS = 'onboarding-pie-gl.questions.json';

async function main(config) {
  await login(config.CARRIER_USERNAME, config.CARRIER_PASSWORD);
  await mfa(config['OTP_SECRET']);
}

function report(status) {
  fs.writeFileSync(path.join(__dirname, 'last-run.json'), JSON.stringify(status));
  console.log('RRSTATUS ' + JSON.stringify(status));
}
