// Answers a gate with a literal, discarding whatever the client said.
const fs = require('fs');
const path = require('path');

const METADATA = 'onboarding-pie-gl.metadata.json';
const QUESTIONS = 'onboarding-pie-gl.questions.json';

async function main(answers) {
  await pickYesNo('No');
  await pickYesNo(answers.answers.hasSubcontractors);
}

function report(status) {
  fs.writeFileSync(path.join(__dirname, 'last-run.json'), JSON.stringify(status));
  console.log('RRSTATUS ' + JSON.stringify(status));
}
