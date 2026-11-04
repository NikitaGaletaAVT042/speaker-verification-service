import jenkins.model.Jenkins

def controller = Jenkins.get()
controller.setNumExecutors(0)
controller.save()
