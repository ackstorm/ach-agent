// SPDX-License-Identifier: Apache-2.0

package v1alpha1

import (
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// HermesAchSpec is where a HermesAgent's ACH credential comes from. The field names and rules are
// the Agent's (AchServerSpec) without its capability fields: Hermes' own config names its models
// and MCP servers.
// +kubebuilder:validation:XValidation:rule="!has(self.keyFrom.tokenExchange) || self.baseUrl.startsWith('https://')",message="ach.baseUrl must be https:// with keyFrom.tokenExchange"
type HermesAchSpec struct {
	// BaseURL is ACH's public URL. With tokenExchange it is also the projected token's audience.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Pattern=`^https?://[^/\s]+`
	BaseURL string `json:"baseUrl"`
	// Environment is the ACH Environment the credential acts in.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Environment string `json:"environment"`
	// +kubebuilder:validation:Required
	KeyFrom AchKeyFrom `json:"keyFrom"`
}

// HermesPersistenceSpec is the PVC that holds Hermes' home: memory, skills, sessions, cron jobs.
type HermesPersistenceSpec struct {
	// +kubebuilder:validation:Required
	Volume WorkspaceVolumeSpec `json:"volume"`
}

// HermesProxySpec is the per-HermesAgent pod that holds the ACH credential.
type HermesProxySpec struct {
	// Image is the ach-agent control image; the proxy runs `ach-runtime credential-proxy`.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Image string `json:"image"`
}

// HermesAgentSpec runs NousResearch Hermes Agent (`hermes gateway run`) as a long-running
// OpenShell sandbox. Its model and MCP calls go through a credential proxy, so no ACH credential
// enters the sandbox.
type HermesAgentSpec struct {
	// +kubebuilder:validation:Required
	Ach HermesAchSpec `json:"ach"`
	// ServiceAccountName runs the proxy pod; with tokenExchange the KeyGrant names it as
	// <namespace>/<serviceAccountName>.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	ServiceAccountName string `json:"serviceAccountName"`
	// +kubebuilder:validation:Required
	Proxy HermesProxySpec `json:"proxy"`
	// Config is Hermes' config.yaml, written to its home at every sandbox start (Hermes' own
	// edits to it do not survive a restart). It reaches the proxy through ${ACH_PROXY_URL} and
	// ${ACH_PROXY_TOKEN}, which Hermes expands.
	// +kubebuilder:validation:Required
	// +kubebuilder:pruning:PreserveUnknownFields
	// +kubebuilder:validation:Schemaless
	// +kubebuilder:validation:Type=object
	Config apiextensionsv1.JSON `json:"config"`
	// SecretRef names a Secret whose keys reach Hermes as environment variables
	// (TELEGRAM_BOT_TOKEN, SLACK_BOT_TOKEN, ...).
	// +optional
	SecretRef *LocalObjectRef `json:"secretRef,omitempty"`
	// +kubebuilder:validation:Required
	Sandbox ProfileSandboxSpec `json:"sandbox"`
	// +kubebuilder:validation:Required
	Persistence HermesPersistenceSpec `json:"persistence"`
}

type HermesAgentStatus struct {
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Namespaced
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=".status.conditions[?(@.type=='Ready')].status"
// +kubebuilder:printcolumn:name="Reason",type=string,JSONPath=".status.conditions[?(@.type=='Ready')].reason"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"

type HermesAgent struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   HermesAgentSpec   `json:"spec,omitempty"`
	Status HermesAgentStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

type HermesAgentList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []HermesAgent `json:"items"`
}

func init() {
	SchemeBuilder.Register(&HermesAgent{}, &HermesAgentList{})
}
