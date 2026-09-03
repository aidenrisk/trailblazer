// Invents values for required fields with `||`. A missing answer must fail the
// stage, never be filled in with a guess.
const fs = require('fs');
const path = require('path');

const METADATA = 'onboarding-pie-gl.metadata.json';
const QUESTIONS = 'onboarding-pie-gl.questions.json';

async function main(answers) {
  await fill('#legalName', answers.answers.legalName || 'Unknown LLC');
  await fill('#payroll', answers.answers['annualPayroll'] || 0);
}

function report(status) {
  fs.writeFileSync(path.join(__dirname, 'last-run.json'), JSON.stringify(status));
  console.log('RRSTATUS ' + JSON.stringify(status));
}
