// Runs before first paint (not deferred): marks that scripts run, so CSS can collapse the mobile menu without a flash.
document.documentElement.classList.add("js");
